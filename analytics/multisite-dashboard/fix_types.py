import re

files = ['auth.py', 'app.py', 'collector.py']

for filename in files:
    with open(filename, 'r') as f:
        content = f.read()
    
    # Add Optional import if not present
    if 'from typing import' in content and 'Optional' not in content:
        content = re.sub(
            r'(from typing import [^\n]+)',
            lambda m: m.group(1) if 'Optional' in m.group(1) else m.group(1).rstrip(')') + ', Optional)',
            content
        )
    elif 'from typing import' not in content:
        content = 'from typing import Optional\n' + content
    
    # Replace type | None with Optional[type]
    content = re.sub(r'(\w+) \| None', r'Optional[\1]', content)
    
    with open(filename, 'w') as f:
        f.write(content)
    
    print(f"Fixed {filename}")

