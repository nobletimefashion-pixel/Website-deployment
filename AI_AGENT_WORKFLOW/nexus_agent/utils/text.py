import tiktoken
def get_tokenizer(model: str):
    try:
        encoding = tiktoken.encoding_for_model(model)
        return encoding.encode
    except Exception:
        try:
            encoding = tiktoken.get_encoding("cl100k_base")
            return encoding.encode
        except Exception:
            return None 
    
def count_token(text: str, model: str = "gpt-4") -> int:
    tokenizer = get_tokenizer(model)
    
    if tokenizer:
        try:
            return len(tokenizer(text))
        except Exception:
            pass
    return estimate_tokens(text)
    
    
def estimate_tokens(text: str) -> int:
    return int(len(text) * 0.25) + 1


def truncate_text(text: str,model:str, max_tokens: int, suffix: str = "\n...[truncated]", preserve_lines: bool = True) -> str:
    current_token_count = count_token(text, model)
    if current_token_count <= max_tokens:
        return text
    
    suffix_token_count = count_token(suffix, model)
    target_tokens = max_tokens - suffix_token_count #in this max_tokens is the maximum number of tokens that we want to keep in the text and suffix_token_count is the number of tokens that are in the suffix. We will subtract the suffix_token_count from the max_tokens to get the target_tokens which is the number of tokens that we want to keep in the text after truncation.
    if target_tokens <= 0:
        return suffix.strip() #if the target_tokens is less than or equal to 0 then we will just return the suffix truncated to the max_tokens as we cannot keep any tokens from the original text.
    if preserve_lines:
        return _truncate_by_lines(text, target_tokens, model, suffix)
    else:
        return _truncate_by_chars(text, target_tokens, model, suffix)
    
def _truncate_by_lines(text: str, target_tokens: int, model: str, suffix: str) -> str:
    """Truncate the text by line boundaries."""
    lines = text.split("\n")
    result_lines: list[str] = []
    current_tokens = 0

    for line in lines:
        line_tokens = count_token(line, model)
        if current_tokens + line_tokens > target_tokens:
            break
        result_lines.append(line)
        current_tokens += line_tokens
    if not result_lines:
        return _truncate_by_chars(text, target_tokens, model, suffix)
    truncated = "\n".join(result_lines)
    return truncated + suffix

def _truncate_by_chars(text: str, target_tokens: int, model: str, suffix: str) -> str:
    """Truncate the text by character count."""
    if get_tokenizer(model) is None:
        approx_chars = target_tokens * 4
        return text[:approx_chars] + suffix
    low, high = 0, len(text)
    while low < high:
        mid = (low + high +1) // 2
        if count_token(text[:mid], model) <= target_tokens:
            low = mid
        else:
            high = mid - 1
    return text[:low] + suffix


#_truncate_by_lines and _truncate_by_chars are helper functions that are used to truncate the text by line boundaries or by character count respectively. The truncate_text function will first check if the current token count of the text is less than or equal to the max_tokens, if it is then it will return the text as it is. If the current token count is greater than the max_tokens then it will calculate the target_tokens by subtracting the suffix_token_count from the max_tokens and then it will call either _truncate_by_lines or _truncate_by_chars based on the preserve_lines parameter to truncate the text accordingly.
#in simple example _truncate_by_lines will try to keep as many lines as possible while keeping the total token count within the target_tokens limit. If it cannot keep any lines then it will fall back to _truncate_by_chars which will truncate the text by character count to fit within the target_tokens limit.
