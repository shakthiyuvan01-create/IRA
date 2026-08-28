import re

def run(args: dict) -> str:
    text = str(args.get('text', ''))
    words = re.findall(r'\S+', text)
    return f'The text contains {len(words)} words.'