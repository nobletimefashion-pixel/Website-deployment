# Tools/builtin/rag.py
import json
import os
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Literal, Optional
import asyncio

from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult
from nexus_agent.utils.path import resolve_path
from nexus_agent.utils.text import truncate_text


class RAGParams(BaseModel):
    action: Literal[
        "ingest",
        "query",
        "list_collections",
        "delete_collection",
        "get_stats"
    ] = Field(
        ...,
        description="Action to perform: ingest PDFs, query documents, or manage collections"
    )
    
    # Ingestion parameters
    directory: str | None = Field(
        None,
        description="Directory containing PDF files to ingest (required for ingest action)"
    )
    collection_name: str | None = Field(
        None,
        description="Name for the vector collection (default: uses directory name)"
    )
    recursive: bool = Field(
        True,
        description="Recursively search subdirectories for PDFs (default: True)"
    )
    
    # Query parameters
    query: str | None = Field(
        None,
        description="Question to ask about the ingested documents (required for query action)"
    )
    top_k: int = Field(
        3,
        ge=1,
        le=10,
        description="Number of relevant chunks to retrieve (default: 3)"
    )
    score_threshold: float = Field(
        0.3,
        ge=0.0,
        le=1.0,
        description="Minimum similarity score threshold (default: 0.3)"
    )
    
    # Processing options
    strategy: Literal["fast", "hi_res"] = Field(
        "fast",
        description="PDF processing strategy: 'fast' or 'hi_res' (default: fast)"
    )
    extract_images: bool = Field(
        False,
        description="Extract and process images from PDFs (default: False)"
    )
    chunk_size: int = Field(
        3000,
        ge=500,
        le=5000,
        description="Maximum characters per chunk (default: 3000)"
    )


class RAGTool(Tool):
    name = "rag"
    description = (
        "Retrieval-Augmented Generation (RAG) system for PDF documents. "
        "Ingest PDFs from directories, create searchable vector collections, "
        "and query documents with natural language questions. "
        "Supports multimodal content (text, tables, images)."
    )
    kind = ToolKind.WRITE
    schema = RAGParams
    
    def __init__(self, config, llm_client):
        super().__init__(config)
        self.llm = llm_client
        self._rag_dir = Path.home() / ".nexus-agent" / "rag"
        self._rag_dir.mkdir(parents=True, exist_ok=True)
        self._embeddings_cache = {}
    
    async def execute(self, invocation: ToolInvokation) -> ToolResult:
        params = RAGParams(**invocation.params)
        
        if params.action == "ingest":
            return await self._ingest_documents(params, invocation.cwd)
        elif params.action == "query":
            return await self._query_documents(params, invocation.cwd)
        elif params.action == "list_collections":
            return await self._list_collections()
        elif params.action == "delete_collection":
            return await self._delete_collection(params)
        elif params.action == "get_stats":
            return await self._get_stats(params)
        
        return ToolResult.error_result(f"Unknown action: {params.action}")
    
    async def _ingest_documents(self, params: RAGParams, cwd: Path) -> ToolResult:
        """Ingest PDF documents from a directory."""
        if not params.directory:
            return ToolResult.error_result("directory is required for ingest action")
        
        dir_path = resolve_path(cwd, params.directory)
        if not dir_path.exists():
            return ToolResult.error_result(f"Directory not found: {dir_path}")
        
        if not dir_path.is_dir():
            return ToolResult.error_result(f"Path is not a directory: {dir_path}")
        
        # Find PDF files
        if params.recursive:
            pdf_files = list(dir_path.rglob("*.pdf"))
        else:
            pdf_files = list(dir_path.glob("*.pdf"))
        
        if not pdf_files:
            return ToolResult.error_result(f"No PDF files found in {dir_path}")
        
        # Determine collection name
        collection_name = params.collection_name or dir_path.name
        collection_dir = self._rag_dir / collection_name
        persist_dir = collection_dir / "chromadb"
        
        try:
            # Import dependencies
            from unstructured.partition.pdf import partition_pdf
            from unstructured.chunking.title import chunk_by_title
            from langchain_chroma import Chroma
            from langchain_community.embeddings import HuggingFaceEmbeddings
            from langchain_core.documents import Document
        except ImportError as e:
            return ToolResult.error_result(
                f"Missing dependencies: {str(e)}\n"
                "Install with: pip install unstructured langchain-chroma langchain-openai chromadb"
            )
        
        output_lines = [
            f"Ingesting PDFs from: {dir_path}",
            f"Found {len(pdf_files)} PDF file(s)",
            f"Collection: {collection_name}\n"
        ]
        
        all_documents = []
        
        for i, pdf_path in enumerate(pdf_files, 1):
            try:
                output_lines.append(f"[{i}/{len(pdf_files)}] Processing: {pdf_path.name}")
                
                # Extract elements from PDF
                elements = partition_pdf(
                    filename=str(pdf_path),
                    strategy=params.strategy,
                    infer_table_structure=True,
                    extract_image_block_types=["Image"] if params.extract_images else [],
                    extract_image_block_to_payload=params.extract_images,
                )
                
                output_lines.append(f"  - Extracted {len(elements)} elements")
                
                # Create chunks
                chunks = chunk_by_title(
                    elements,
                    max_characters=params.chunk_size,
                    new_after_n_chars=int(params.chunk_size * 0.8),
                    combine_text_under_n_chars=500,
                )
                
                output_lines.append(f"  - Created {len(chunks)} chunks")
                
                # Convert to LangChain documents
                for chunk in chunks:
                    content_data = self._analyze_chunk(chunk)
                    
                    doc = Document(
                        page_content=content_data['text'],
                        metadata={
                            "source": str(pdf_path),
                            "filename": pdf_path.name,
                            "collection": collection_name,
                            "types": content_data['types'],
                            "has_tables": len(content_data['tables']) > 0,
                            "has_images": len(content_data['images']) > 0,
                        }
                    )
                    all_documents.append(doc)
                
            except Exception as e:
                output_lines.append(f"  - Error: {str(e)}")
                continue
        
        if not all_documents:
            return ToolResult.error_result(
                "\n".join(output_lines) + "\n\n No documents were successfully processed"
            )
        
        # Create vector store
        try:
            output_lines.append(f"\n Creating vector embeddings...")
            
            from langchain_community.embeddings import HuggingFaceEmbeddings

            embedding_model = HuggingFaceEmbeddings(
                model_name="BAAI/bge-small-en-v1.5"
            )
            vector_store = Chroma.from_documents(
                documents=all_documents,
                embedding=embedding_model,
                persist_directory=str(persist_dir),
                collection_metadata={"hnsw:space": "cosine"}
            )
            
            output_lines.append(f"Vector store created: {persist_dir}")
            
            # Save metadata
            metadata = {
                "collection_name": collection_name,
                "source_directory": str(dir_path),
                "pdf_count": len(pdf_files),
                "document_count": len(all_documents),
                "strategy": params.strategy,
                "extract_images": params.extract_images,
                "chunk_size": params.chunk_size,
            }
            
            metadata_path = collection_dir / "metadata.json"
            with open(metadata_path, "w") as f:
                json.dump(metadata, f, indent=2)
            
            output = "\n".join(output_lines)
            
            return ToolResult.success_result(
                output=output,
                metadata={
                    "collection_name": collection_name,
                    "pdf_count": len(pdf_files),
                    "document_count": len(all_documents),
                    "persist_directory": str(persist_dir)
                }
            )
        
        except Exception as e:
            return ToolResult.error_result(
                "\n".join(output_lines) + f"\n\n Error creating vector store: {str(e)}"
            )
    
    async def _query_documents(self, params: RAGParams, cwd: Path) -> ToolResult:
        """Query ingested documents."""
        if not params.query:
            return ToolResult.error_result("query is required for query action")
        
        if not params.collection_name:
            return ToolResult.error_result("collection_name is required for query action")
        
        collection_dir = self._rag_dir / params.collection_name
        persist_dir = collection_dir / "chromadb"
        
        if not persist_dir.exists():
            return ToolResult.error_result(
                f"Collection '{params.collection_name}' not found. "
                f"Use action='list_collections' to see available collections."
            )
        
        try:
            from langchain_chroma import Chroma
            from langchain_community.embeddings import HuggingFaceEmbeddings
        except ImportError as e:
            return ToolResult.error_result(
                f"Missing dependencies: {str(e)}\n"
                "Install with: pip install langchain-chroma langchain-openai chromadb"
            )
        
        try:
            # Load vector store
            embedding_model = HuggingFaceEmbeddings(
                model_name="BAAI/bge-small-en-v1.5"
            )
            
            vector_store = Chroma(
                persist_directory=str(persist_dir),
                embedding_function=embedding_model
            )
            
            # Retrieve relevant documents
            retriever = vector_store.as_retriever(
                search_type="similarity_score_threshold",
                search_kwargs={
                    "k": params.top_k,
                    "score_threshold": params.score_threshold
                }
            )
            
            docs = retriever.invoke(params.query)
            
            if not docs:
                return ToolResult.success_result(
                    f"No relevant documents found for query: '{params.query}'",
                    metadata={
                        "query": params.query,
                        "collection": params.collection_name,
                        "documents_found": 0
                    }
                )
            
            # Generate answer using LLM
            prompt_text = f"""Based on the following documents, please answer this question: {params.query}

RETRIEVED DOCUMENTS:

"""
            for i, doc in enumerate(docs, 1):
                source = doc.metadata.get('filename', 'Unknown')
                prompt_text += f"--- Document {i} (Source: {source}) ---\n"
                truncated = truncate_text(doc.page_content, model="gpt-4", max_tokens=2000)
                prompt_text += truncated + "\n\n"
            
            prompt_text += """
Please provide a comprehensive answer based on the documents above. If the documents don't contain 
sufficient information to answer the question, clearly state that.

ANSWER:"""
            
            messages = [
                {"role": "user", "content": prompt_text}
            ]
            
            answer = ""
            
            async for event in self.llm.chat_completion(
                messages,
                stream=False
            ):
                if event.text_delta:
                    answer = event.text_delta.content
            
            # Format output
            output_lines = [
                f"Query: {params.query}",
                f"Collection: {params.collection_name}",
                f"Retrieved {len(docs)} relevant document(s)\n",
                "=" * 80,
                answer,
                "=" * 80,
                "\nSources:"
            ]
            
            for i, doc in enumerate(docs, 1):
                source = doc.metadata.get('filename', 'Unknown')
                preview = doc.page_content[:150].replace('\n', ' ')
                output_lines.append(f"  [{i}] {source}: {preview}...")
            
            output = "\n".join(output_lines)
            
            return ToolResult.success_result(
                output=output,
                metadata={
                    "query": params.query,
                    "collection": params.collection_name,
                    "documents_found": len(docs),
                    "sources": [doc.metadata.get('filename', 'Unknown') for doc in docs],
                    "answer": answer
                }
            )
        
        except Exception as e:
            return ToolResult.error_result(
                f"Query failed: {str(e)}",
                metadata={"query": params.query, "collection": params.collection_name}
            )
    
    async def _list_collections(self) -> ToolResult:
        """List all available RAG collections."""
        collections = []
        
        if not self._rag_dir.exists():
            return ToolResult.success_result(
                "No RAG collections found.",
                metadata={"collections": []}
            )
        
        for collection_dir in self._rag_dir.iterdir():
            if not collection_dir.is_dir():
                continue
            
            metadata_path = collection_dir / "metadata.json"
            if metadata_path.exists():
                try:
                    with open(metadata_path) as f:
                        metadata = json.load(f)
                    collections.append(metadata)
                except:
                    continue
        
        if not collections:
            return ToolResult.success_result(
                "No RAG collections found.",
                metadata={"collections": []}
            )
        
        output_lines = [f"Found {len(collections)} RAG collection(s):\n"]
        
        for coll in collections:
            output_lines.append(f"- {coll['collection_name']}")
            output_lines.append(f"  - Source: {coll['source_directory']}")
            output_lines.append(f"  - PDFs: {coll['pdf_count']}, Chunks: {coll['document_count']}")
            output_lines.append(f"  - Strategy: {coll['strategy']}\n")
        
        output = "\n".join(output_lines)
        
        return ToolResult.success_result(
            output=output,
            metadata={"collections": [c['collection_name'] for c in collections]}
        )
    
    async def _delete_collection(self, params: RAGParams) -> ToolResult:
        """Delete a RAG collection."""
        if not params.collection_name:
            return ToolResult.error_result("collection_name is required for delete_collection action")
        
        collection_dir = self._rag_dir / params.collection_name
        
        if not collection_dir.exists():
            return ToolResult.error_result(f"Collection '{params.collection_name}' not found")
        
        try:
            import shutil
            shutil.rmtree(collection_dir)
            
            return ToolResult.success_result(
                f"Deleted collection: {params.collection_name}",
                metadata={"deleted_collection": params.collection_name}
            )
        except Exception as e:
            return ToolResult.error_result(f"Failed to delete collection: {str(e)}")
    
    async def _get_stats(self, params: RAGParams) -> ToolResult:
        """Get statistics for a collection."""
        if not params.collection_name:
            return ToolResult.error_result("collection_name is required for get_stats action")
        
        collection_dir = self._rag_dir / params.collection_name
        metadata_path = collection_dir / "metadata.json"
        
        if not metadata_path.exists():
            return ToolResult.error_result(f"Collection '{params.collection_name}' not found")
        
        try:
            with open(metadata_path) as f:
                metadata = json.load(f)
            
            output_lines = [
                f"Statistics for '{params.collection_name}':\n",
                f"Source Directory: {metadata['source_directory']}",
                f"PDF Files: {metadata['pdf_count']}",
                f"Total Chunks: {metadata['document_count']}",
                f"Processing Strategy: {metadata['strategy']}",
                f"Image Extraction: {'Enabled' if metadata['extract_images'] else 'Disabled'}",
                f"Chunk Size: {metadata['chunk_size']} characters"
            ]
            
            return ToolResult.success_result(
                output="\n".join(output_lines),
                metadata=metadata
            )
        except Exception as e:
            return ToolResult.error_result(f"Failed to get stats: {str(e)}")
    
    def _analyze_chunk(self, chunk) -> dict:
        """Analyze chunk content types."""
        content_data = {
            "text": chunk.text,
            "tables": [],
            "images": [],
            "types": ['text']
        }
        
        if hasattr(chunk, 'metadata') and hasattr(chunk.metadata, 'orig_elements'):
            for element in chunk.metadata.orig_elements:
                element_type = type(element).__name__
                
                if element_type == 'Table':
                    content_data["types"].append('table')
                    table_html = getattr(element.metadata, 'text_as_html', element.text)
                    content_data["tables"].append(table_html)
                
                elif element_type == 'Image':
                    if hasattr(element, 'metadata') and hasattr(element.metadata, 'image_base64'):
                        content_data["types"].append('image')
                        content_data["images"].append(element.metadata.image_base64)
        
        content_data["types"] = list(set(content_data["types"]))
        return content_data