# Hybrid AI System Setup Guide

## Overview

DBBuddy now supports a hybrid AI system with intelligent fallback capabilities:

- **Primary**: Qwen Coder (local via Ollama)
- **Fallback**: Nemotron 3 Ultra (API via NVIDIA)

This architecture provides:
- **Zero/near-zero cost**: Local model handles most queries
- **Strong SQL capability**: Both models are code-first and optimized for SQL generation
- **Real fallback testing**: Validates fallback logic with actual model responses
- **Model tracking**: Every response includes which model was used

## Architecture

```
User Query
   ↓
Qwen Coder (local)
   ↓
clean_sql_output()
   ↓
is_valid_sql()
   ↓
✔ valid → execute
❌ invalid / timeout →
        ↓
    Nemotron API
        ↓
    clean + validate
        ↓
    execute
```

## Environment Variables

### Required for Local Model (Qwen Coder)

```bash
# Local model name (default: qwen2.5-coder:7b)
LOCAL_MODEL=qwen2.5-coder:7b

# Ollama must be running at: http://127.0.0.1:11434
```

### Required for Nemotron Fallback

```bash
# NVIDIA API key for Nemotron 3 Ultra
NEMOTRON_API_KEY=your_nemotron_api_key_here

# Optional: Custom endpoint (default: NVIDIA's API)
NEMOTRON_ENDPOINT=https://integrate.api.nvidia.com/v1/chat/completions
```

## Installation

### 1. Install Ollama and Qwen Coder

```bash
# Install Ollama (if not already installed)
# Visit: https://ollama.ai

# Check available models
ollama list

# Pull Qwen Coder model (if not already present)
ollama pull qwen2.5-coder:7b

# Start Ollama server
ollama serve
```

### 2. Get Nemotron API Key

1. Visit [NVIDIA API Catalog](https://build.nvidia.com/)
2. Sign up and get API key for Nemotron 3 Ultra
3. Set environment variable:
   ```bash
   export NEMOTRON_API_KEY=your_key_here
   ```

### 3. Configure DBBuddy

```python
from dbbuddy_core.models import DBConfig
from dbbuddy_core.pipeline import process_query

config = DBConfig(
    host="localhost",
    user="your_user",
    password="your_password",
    database="your_database",
    ai=True,
    ai_provider="hybrid",  # Use hybrid mode for automatic fallback
    fallback_provider="nemotron",  # Fallback to Nemotron
)

response = process_query(config, user_query="Show me all users")
print(f"Model used: {response['model_used']}")
print(f"SQL: {response['sql']}")
```

## Provider Options

### `ai_provider` Options

- `"local"`: Use only Qwen Coder (Ollama)
- `"nemotron"`: Use only Nemotron 3 Ultra API
- `"hybrid"`: Automatic fallback chain: local → nemotron

### `fallback_provider` Options

- `"nemotron"`: Fallback to Nemotron 3 Ultra (default)

## Response Metadata

Every query response now includes:

```python
{
    "query": "user question",
    "sql": "generated sql",
    "query_type": "select",
    "model_used": "local",  # or "nemotron", "unknown"
    "auto_executed": True,
    "results": [...],
    "confidence": "high"
}
```

## Validation Improvements

The `is_valid_sql()` function now includes:

- **Keyword validation**: Only allows SELECT, INSERT, UPDATE, DELETE, WITH, SHOW, EXPLAIN
- **Safety checks**: Blocks DROP and TRUNCATE operations
- **Empty string rejection**: Prevents empty or malformed responses

## Timeout-Based Fallback

The hybrid provider implements intelligent fallback:

1. **Local timeout**: If Ollama times out (>30s), fallback to Nemotron
2. **Invalid SQL**: If local model produces invalid SQL, fallback to Nemotron
3. **Empty response**: If local model returns empty/unknown, fallback to Nemotron
4. **API failure**: If Nemotron fails, returns unknown

## Cost Analysis

| Provider | Cost | Latency | Use Case |
|----------|------|---------|----------|
| Qwen Coder (local) | Free | Fast (~2-5s) | Primary - 80-90% of queries |
| Nemotron 3 Ultra | Free (NVIDIA) | Medium (~3-8s) | Fallback - 10-15% of queries |

## Testing the Hybrid System

### Test Local Model Only

```python
config = DBConfig(
    # ... other config ...
    ai_provider="local"
)
```

### Test Nemotron Only

```python
config = DBConfig(
    # ... other config ...
    ai_provider="nemotron"
)
```

### Test Hybrid Fallback

```python
config = DBConfig(
    # ... other config ...
    ai_provider="hybrid",
    fallback_provider="nemotron"
)

# Stop Ollama to test fallback
# The system should automatically use Nemotron
```

## Troubleshooting

### Ollama Not Running

```
Warning: Ollama not running. Falling back to Nemotron if available.
```

**Solution**: Start Ollama with `ollama serve`

### Nemotron API Key Missing

```
Model used: unknown
```

**Solution**: Set `NEMOTRON_API_KEY` environment variable

### Model Not Found

```
Error: model 'qwen2.5-coder:7b' not found
```

**Solution**: Check available models with `ollama list` and pull if needed

### All Fallbacks Failed

```
SQL generation failed. The model could not produce a valid query.
```

**Solution**: Check API key and network connectivity

## Performance Tuning

### Adjust Local Model

```bash
# Use smaller model for faster responses
LOCAL_MODEL=qwen2.5-coder:3b

# Use larger model for better accuracy
LOCAL_MODEL=qwen2.5-coder:14b
```

### Adjust Timeouts

Edit `dbbuddy_core/query.py` to modify timeout values:

```python
# In generate_sql_local()
timeout=30  # Increase for slower hardware

# In generate_sql_nemotron()
timeout=30  # Increase for slower networks
```

## Monitoring Model Usage

Track which models are being used:

```python
response = process_query(config, user_query="...")
print(f"Model: {response['model_used']}")
print(f"Confidence: {response['confidence']}")

# Log model usage for analytics
if response['model_used'] == 'nemotron':
    logger.warning("Fallback to Nemotron triggered")
```

## Best Practices

1. **Start with hybrid mode**: Let the system automatically choose the best model
2. **Monitor fallback rate**: High fallback rate indicates local model issues
3. **Keep Ollama running**: Ensure local model is available for best performance
4. **Set Nemotron API key**: Required for reliable fallback
5. **Review model_used field**: Track which model handled each query
6. **Test with bad queries**: Validate fallback logic with edge cases

## Security Notes

- **SQL Injection**: The validation layer blocks DROP/TRUNCATE operations
- **API Keys**: Never commit API keys to version control
- **Local Model**: Runs entirely on your machine - no data leaves
- **Nemotron API**: Data sent to NVIDIA servers - review their privacy policy

## Future Enhancements

Potential improvements:

- [ ] Add caching for repeated queries
- [ ] Implement model performance metrics
- [ ] Add support for other local models (CodeLlama, etc.)
- [ ] Implement query complexity detection
- [ ] Add rate limiting for API calls
- [ ] Support for custom fallback chains

## Support

For issues or questions:

1. Check this documentation
2. Review logs in `logs/` directory
3. Verify environment variables are set
4. Test each provider independently
5. Check network connectivity for API calls
