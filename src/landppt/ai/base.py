"""
Base classes for AI providers
"""

from abc import ABC, abstractmethod
from contextlib import aclosing
from typing import List, Dict, Any, Optional, AsyncGenerator, Union
from pydantic import BaseModel, Field
from enum import Enum

class MessageRole(str, Enum):
    """Message roles for AI conversations"""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"

class MessageContentType(str, Enum):
    """Message content types for multimodal support"""
    TEXT = "text"
    IMAGE_URL = "image_url"

class ImageContent(BaseModel):
    """Image content for multimodal messages"""
    type: MessageContentType = MessageContentType.IMAGE_URL
    image_url: Dict[str, str]  # {"url": "data:image/jpeg;base64,..." or "http://..."}

class TextContent(BaseModel):
    """Text content for multimodal messages"""
    type: MessageContentType = MessageContentType.TEXT
    text: str

class AIMessage(BaseModel):
    """AI message model with multimodal support"""
    role: MessageRole
    content: Union[str, List[Union[TextContent, ImageContent]]]  # Support both simple string and multimodal content
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None

class AIResponse(BaseModel):
    """AI response model"""
    content: str
    model: str
    usage: Dict[str, int]
    finish_reason: Optional[str] = None
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

class AIProvider(ABC):
    """Abstract base class for AI providers"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.model = config.get("model", "unknown")
    
    @abstractmethod
    async def chat_completion(
        self,
        messages: List[AIMessage],
        **kwargs
    ) -> AIResponse:
        """Generate chat completion"""
        pass
    
    @abstractmethod
    async def text_completion(
        self,
        prompt: str,
        **kwargs
    ) -> AIResponse:
        """Generate text completion"""
        pass
    
    async def stream_chat_completion(
        self,
        messages: List[AIMessage],
        **kwargs
    ) -> AsyncGenerator[str, None]:
        """Stream chat completion (optional)"""
        # Default implementation: return full response at once
        response = await self.chat_completion(messages, **kwargs)
        yield response.content
    
    async def stream_text_completion(
        self,
        prompt: str,
        **kwargs
    ) -> AsyncGenerator[str, None]:
        """Stream text completion (optional)"""
        # Default implementation: return full response at once
        response = await self.text_completion(prompt, **kwargs)
        yield response.content

    async def collect_streamed_chat_completion(
        self, messages: List[AIMessage], **kwargs
    ) -> AIResponse:
        """Collect a provider's text stream; unavailable usage is not estimated."""
        on_chunk = kwargs.pop("on_chunk", None)
        chunks = []
        async with aclosing(self.stream_chat_completion(messages, **kwargs)) as stream:
            async for chunk in stream:
                chunks.append(chunk)
                if on_chunk is not None:
                    await on_chunk(chunk)
        content = "".join(chunks)
        if not content.strip():
            raise ValueError("模型流式响应为空")
        return AIResponse(
            content=content,
            model=kwargs.get("model") or self.model,
            usage={},
            metadata={"streamed": True, "usage_available": False},
        )
    
    def get_model_info(self) -> Dict[str, Any]:
        """Get model information"""
        return {
            "model": self.model,
            "provider": self.__class__.__name__,
            "config": {k: v for k, v in self.config.items() if "key" not in k.lower()}
        }
    
    def _calculate_usage(self, prompt: str, response: str) -> Dict[str, int]:
        """Calculate token usage (simplified)"""
        # Simplified calculation
        prompt_tokens = len(prompt.split())
        completion_tokens = len(response.split())
        
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens
        }
    
    def _merge_config(self, **kwargs) -> Dict[str, Any]:
        """
        Merge provider config with request parameters.

        Note:
        - In this project, `MAX_TOKENS/max_tokens` refers to the *chunking/splitting* limit.
          It must not be forwarded to model providers as an output length constraint.
        - Output token caps are intentionally not forwarded to model providers.
        """
        merged = self.config.copy()
        merged.pop("max_tokens", None)
        merged.pop("max_output_tokens", None)

        # Drop output length controls to avoid coupling chunking/user config to model requests.
        kwargs.pop("max_tokens", None)
        kwargs.pop("max_output_tokens", None)
        merged.update(kwargs)

        return merged
