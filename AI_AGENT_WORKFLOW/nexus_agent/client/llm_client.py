import asyncio
from openai import APIConnectionError, APIError, AsyncOpenAI
from typing import Any, AsyncGenerator
from openai import RateLimitError


from nexus_agent.client.response import StreamEvent, TextDelta, TokenUsage, StreamEventType, ToolCall, ToolCallDelta, parse_tool_call_arguments
from nexus_agent.config.config import Config

class LLMClient:
    #creating the connection
    def __init__(self,config: Config) -> None:
        self._client : AsyncOpenAI | None = None
        self._max_retries:int = 3
        self.config = config
    #getting the client
    def get_client(self)-> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.config.api_key, 
                base_url=self.config.base_url, 
            )
        return self._client
    #closing the connection
    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
    def _built_tools(self,tools: list[dict[str, Any]]):
        built = [
            {
                'type': 'function',
                'function': {
                    'name': tool['name'],
                    'description': tool.get('description',''),
                    'parameters': tool.get('parameters',{'type': 'object','properties':{}})
                }
            }
            for tool in tools
        ]
        return built
            
    async def chat_completion(self, messages: list[dict[str, Any]],tools: list[dict[str, Any]] | None=None,stream: bool = True) -> AsyncGenerator:
        
        client = self.get_client()
        kwargs = {
            "model": self.config.model_name,
            "messages": messages,
            "stream": stream,
                }
        if tools:
            kwargs['tools'] = self._built_tools(tools)
            kwargs['tool_choice'] = "auto" #we can force any particaular tool or make it auto
        for attempt in range(self._max_retries + 1):
            try:
                if stream:
                    async for event in self._stream_response(client, kwargs):
                        yield event
                else:
                    event = await self._non_stream_response(client, kwargs)
                    yield event
                return
            except RateLimitError as e:
                if attempt < self._max_retries:
                    wait_time = 2**attempt#exponential backoff
                    await asyncio.sleep(wait_time)
                else:
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        error=f"The Ratelimit exceeded: {e}",
                    )
                    return
            except APIConnectionError as e:
                if attempt < self._max_retries:
                    wait_time = 2**attempt#exponential backoff
                    await asyncio.sleep(wait_time)
                else:
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        error=f"Connection Error: {e}",
                    )
                    return
            except APIError as e:
                yield StreamEvent(
                    type=StreamEventType.ERROR,
                    error=f"Api error: {e}",
                    )
                return
            
            

    async def _stream_response(self, client: AsyncOpenAI, kwargs: dict[str, Any]) -> AsyncGenerator[StreamEvent, None]:
        response = await client.chat.completions.create(**kwargs)
        finish_reason: str | None = None
        usage: TokenUsage | None = None
        tool_calls: dict[int, dict[str,Any]] = {}
        async for chunck in response:
            if hasattr(chunck, "usage") and chunck.usage:
                usage = TokenUsage(
                    prompt_tokens=chunck.usage.prompt_tokens,
                    completion_tokens=chunck.usage.completion_tokens,
                    total_tokens=chunck.usage.total_tokens,
                    cached_tokens=(
                        chunck.usage.prompt_tokens_details.cached_tokens
                        if chunck.usage.prompt_tokens_details
                        else 0       
                 )
                )
            if not chunck.choices:
                continue
            choice = chunck.choices[0]
            delta = choice.delta
            
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            
            if delta.content:
                yield StreamEvent(
                    type=StreamEventType.TEXT_DELTA,
                    text_delta=TextDelta(content=delta.content),
                )
            if delta.tool_calls:
                for tool_call_delta in delta.tool_calls:
                    idx = tool_call_delta.index
                    
                    if idx not in tool_calls:
                        tool_calls[idx] = {
                            'id':tool_call_delta.id or "",
                            'name': "",
                            'arguments': ""
                            
                        }
                    if tool_call_delta.function:
                        if tool_call_delta.function.name:
                            tool_calls[idx]['name'] = tool_call_delta.function.name
                            yield StreamEvent(
                                type=StreamEventType.TOOL_CALL_START,
                                tool_call_delta=ToolCallDelta(
                                    call_id=tool_calls[idx]['id'],
                                    name=tool_call_delta.function.name
                                )
                            )
                        if tool_call_delta.function.arguments:
                            tool_calls[idx]['arguments'] += tool_call_delta.function.arguments
                            yield StreamEvent(
                                type=StreamEventType.TOOL_CALL_DELTA,
                                tool_call_delta=ToolCallDelta(
                                    call_id=tool_calls[idx]['id'],
                                    name=tool_call_delta.function.name,
                                    arguments_delta=tool_call_delta.function.arguments
                                )
                            )
    
        for idx, tr in tool_calls.items():
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL_COMPLETE,
                tool_call=ToolCall(
                    call_id=tr['id'],
                    name=tr['name'],
                    arguments=parse_tool_call_arguments(tr['arguments'])
                )
                )
                
        yield StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            finish_reason=finish_reason,
            usage=usage
        )
    
    #gets keyword arguments and sends a non-streaming response to the client
    async def _non_stream_response(self, client: AsyncOpenAI, kwargs: dict[str, Any]) -> StreamEvent:
        response = await client.chat.completions.create(**kwargs)
        choices = response.choices[0]
        message = choices.message
        text_delta = None
        if message.content:
            text_delta = TextDelta(content=message.content)
        usage = None
        
        tool_calls: list[ToolCall] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append(ToolCall(
                    call_id=tc.id,
                    name=tc.function.name,
                    arguments=parse_tool_call_arguments(tc.function.arguments)
                ))
        if response.usage:
            usage = TokenUsage(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
                cached_tokens=response.usage.prompt_tokens_details.cached_tokens,
            )
        return StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            text_delta=text_delta,
            finish_reason=choices.finish_reason,
            usage=usage
        )
        
