from sys import path
from pydantic import BaseModel, Field

from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult
from nexus_agent.utils.path import is_binary_file, resolve_path
from nexus_agent.utils.text import count_token, truncate_text

#we will not use dataclasses as they are used for everything other then tool calling parameters and results as they do not have the functionality that pydantic models have like validation and converting to json schema format which is required by openai api to display the tool in the tool box. So we will use pydantic models for the parameters and results of the tools and we will use dataclasses for everything else like ToolInvokation, ToolConfirmation and ToolResult.
class ReadFileParams(BaseModel):
# here we will call the parameter class ReadFileParams which will be used to validate the parameters that are being passed to the tool when it is being executed. This class will inherit from BaseModel which is a pydantic model that will help us in validating the parameters and also in converting the model to json schema format which is required by openai api to display the tool in the tool box.
    path: str = Field(..., description="The path to the file to read")
    offset: int = Field(1, ge=1, description="The offset from the start of the file to read from. Default is 1.")
    limit: int | None = Field(None, ge=1, description="The maximum number of lines to read from the file.if not specified reads entire file")


class ReadFileTool(Tool):
    name = "read_file"
    #descriptions are important as they are given to an llm to understand what the tool does and how to use it. So we should always provide a good description for our tools.
    description = ("Reads a content of text file and returns file contents with line numbers"
                   "For large files, use offset and limit parameters to read specific portions."
                   "Cannot read binary files and images")
    kind = ToolKind.READ
    #this schema property is used to define the parameters that are required to execute the tool. This will be used by the openai api to validate the parameters that are being passed to the tool when it is being executed and also to display the tool in the tool box with the correct parameters and their descriptions.
    schema = ReadFileParams
    MAX_FILE_SIZE = 10 * 1024 * 1024 #10 MB
    MAX_TOKEN_COUNT = 250000
    async def execute(self, invokation: ToolInvokation) -> ToolResult:
        #here params is the parameters that are being passed to the tool when it is being executed. We will use the ReadFileParams class to validate
        #and **invokation.params is deconstructing the dictionary of parameters that are being passed to the tool when it is being executed and passing it to the ReadFileParams class to validate
        params = ReadFileParams(**invokation.params)
        file_path = resolve_path(invokation.cwd, params.path)#in this invokation is the current working directory of the tool that is being executed and params.path is the path that is being passed to the tool when it is being executed. This function will resolve the path to an absolute path by joining the base and the path if the path is not absolute. If the path is already absolute then it will return the path as it is.

        if not file_path.exists():
            return ToolResult.error_result(f"File {file_path} does not exist")
        if not file_path.is_file():
            return ToolResult.error_result(f"Path {file_path} is not a file")
        
        file_size = file_path.stat().st_size#checks size
        if file_size > self.MAX_FILE_SIZE:
            return ToolResult.error_result(
                f"File {file_path} is too large to read (size: {file_size}). "
                f"Maximum allowed size is {self.MAX_FILE_SIZE} bytes."
            )
        if is_binary_file(file_path):
            file_size_mb = file_size / (1024 * 1024)
            size_str = f"{file_size_mb:.2f} MB" if file_size_mb >= 1 else f"{file_size / 1024:.2f} KB"
            return ToolResult.error_result(
                f"File {file_path} is a binary file and cannot be read (size: {size_str})"
                f"This tool is designed to read text files only. Please provide a valid text file."
            )
        try:
            try:
               content = file_path.read_text(encoding='utf-8')
            except UnicodeDecodeError:
               content = file_path.read_text(encoding='latin-1')
            lines = content.splitlines()#this will split the content of the file into lines and return a list of lines. This is useful for reading large files as we can read the file line by line instead of reading the entire file at once which can cause memory issues for large files.
            total_lines = len(lines)
            if total_lines == 0:
                return ToolResult.success_result('file is empty', metadata={"total_lines": 0})
    
            start_idx = max(0, params.offset - 1)  # this is the index of the line from which we will start reading the file. We will use max function to ensure that the offset is at least 1 and then we will subtract 1 from it to get the correct index as the list of lines is 0 indexed.
            if params.limit is not None:
                end_idx = min(start_idx + params.limit, total_lines)  # this is the index of the line at which we will stop reading the file. We will use min function to ensure that we do not go beyond the total number of lines in the file.
            else:
                end_idx = total_lines  # if limit is not specified then we will read the entire file from the offset to the end of the file.
            selected_lines = lines[start_idx:end_idx]  # this will give us the list of lines that we need to read from the file based on the offset and limit parameters that are passed to the tool when it is being executed.
            #we will also add line numbers to the selected lines to make it easier for the user
            formated_lines = []
            for i, line in enumerate(selected_lines, start=start_idx + 1):
                formated_lines.append(f"{i:6}|{line}")
            output = "\n".join(formated_lines)
            token_count = count_token(output)
            Truncation = False
            if token_count > self.MAX_TOKEN_COUNT:
                output = truncate_text(output, self.MAX_TOKEN_COUNT, suffix="\n...[truncated]", preserve_lines=True)
                Truncation = True
            metadata_lines = []
            if start_idx > 0 or end_idx < total_lines:
                metadata_lines.append(f"Showing lines {start_idx + 1} to {end_idx} of {total_lines} total lines.")
                #this metadata_lines will tell llm and us about the lines that are being shown to us and if there are more lines in the file that are not being shown to us. This is useful for large files as we can read the file in chunks and keep track of which lines we have read and which lines we have not read yet.
            if metadata_lines:
                headers = " | ".join(metadata_lines) + "\n\n"
                output = headers + output
                
            return ToolResult.success_result(
                output=output,
                truncated=Truncation,
                metadata={#metadata is useful to display it to the user and also to llm as it can help llm to understand the context of the file that is being read and also to keep track of which lines have been read and which lines have not been read yet.
                    "path": str(file_path),
                    "total_lines": total_lines,
                    "shown_start": start_idx + 1,
                    "shown_end": end_idx,
                    "truncated": Truncation,
                }
            )
        except Exception as e:
            return ToolResult.error_result(f"An error occurred while reading the file: {str(e)}")