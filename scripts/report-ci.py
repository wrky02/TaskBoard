"""Publish redacted test diagnostics as a workflow annotation (no runtime data)."""
import re
from pathlib import Path
import sys
text=Path(sys.argv[1]).read_text(encoding='utf-8',errors='replace')[-24000:]
failures=list(re.finditer(r'(?m)^(?:ERROR|FAIL):\s|^Traceback \(most recent call last\):|^FAILED \(',text))
if failures:
    # GitHub check annotations have a small display limit. Keep the unittest
    # failure context instead of allowing pip download chatter to hide it.
    first=next((m for m in failures if text[m.start():].startswith(('ERROR:','FAIL:'))),failures[0])
    text=text[first.start():]
    if len(text)>7000:text=text[:3500]+'\n… failure details truncated …\n'+text[-3500:]
else:
    text=text[-7000:]
text=re.sub(r'[A-Za-z0-9_-]{40,}', '[redacted]',text)
text=re.sub(r'http://127\.0\.0\.1:\d+/#\S+', '[launch URL redacted]', text)
escaped=text.replace('%','%25').replace('\r','%0D').replace('\n','%0A')
title=sys.argv[2] if len(sys.argv)>2 else 'Browser regression'
print('::error title='+title+'::'+escaped)
