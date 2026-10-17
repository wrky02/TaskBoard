"""Publish redacted test diagnostics as a workflow annotation (no runtime data)."""
import re
from pathlib import Path
import sys
text=Path(sys.argv[1]).read_text(encoding='utf-8',errors='replace')[-12000:]
text=re.sub(r'[A-Za-z0-9_-]{40,}', '[redacted]',text)
text=re.sub(r'http://127\.0\.0\.1:\d+/#\S+', '[launch URL redacted]', text)
escaped=text.replace('%','%25').replace('\r','%0D').replace('\n','%0A')
print('::error title=Browser regression::'+escaped)
