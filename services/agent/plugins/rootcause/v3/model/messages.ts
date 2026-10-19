// The one model call a move makes, in the Anthropic Messages shape: the shape the loop was
// built and tuned on, with prompt caching on the system prompt and the last message,
// adaptive thinking, and a forced submit tool. Only what the loop sends and reads is typed
// here. Whatever carries the call to the model implements Messages.

export interface CacheControl {
  type: "ephemeral";
}

export interface TextBlock {
  type: "text";
  text: string;
  cache_control?: CacheControl;
}

export interface ThinkingBlock {
  type: "thinking";
  thinking: string;
  signature?: string;
}

export interface ToolUseBlock {
  type: "tool_use";
  id: string;
  name: string;
  input: unknown;
}

export interface ToolResultBlock {
  type: "tool_result";
  tool_use_id: string;
  content: string;
  is_error?: boolean;
  cache_control?: CacheControl;
}

export type ContentBlock = TextBlock | ThinkingBlock | ToolUseBlock;

export interface MessageParam {
  role: "user" | "assistant";
  content: string | (ContentBlock | ToolResultBlock)[];
}

export interface Tool {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
}

export interface MessageRequest {
  model: string;
  max_tokens: number;
  system: TextBlock[];
  tools: Tool[];
  messages: MessageParam[];
  tool_choice?: { type: "tool"; name: string };
  thinking?: { type: "adaptive"; display: "summarized" };
}

export interface Message {
  content: ContentBlock[];
  stop_reason: string | null;
  usage: {
    input_tokens: number;
    output_tokens: number;
    cache_read_input_tokens?: number | null;
    cache_creation_input_tokens?: number | null;
  };
}

export interface Messages {
  create(request: MessageRequest): Promise<Message>;
}
