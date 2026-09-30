from pathlib import Path
import textwrap

source = Path('generated_context.txt').read_text(encoding='utf-8')
lines = []
for paragraph in source.splitlines():
    lines.extend(textwrap.wrap(paragraph, 94) or [''])
pages = [lines[i:i + 56] for i in range(0, len(lines), 56)]
objects = []
def add(value):
    objects.append(value)
    return len(objects)

font = add('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
contents = []
for page in pages:
    commands = ['BT', '/F1 9 Tf', '48 750 Td', '12 TL']
    for line in page:
        safe = line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
        commands.append(f'({safe}) Tj T*')
    commands.append('ET')
    stream = '\n'.join(commands)
    contents.append(add(f'<< /Length {len(stream.encode("latin-1", "replace"))} >>\nstream\n{stream}\nendstream'))

pages_id = add('')
page_ids = [add(f'<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>') for content in contents]
objects[pages_id - 1] = f'<< /Type /Pages /Kids [{" ".join(str(x) + " 0 R" for x in page_ids)}] /Count {len(page_ids)} >>'
catalog = add(f'<< /Type /Catalog /Pages {pages_id} 0 R >>')

pdf = bytearray(b'%PDF-1.4\n')
offsets = [0]
for number, value in enumerate(objects, 1):
    offsets.append(len(pdf))
    pdf.extend(f'{number} 0 obj\n{value}\nendobj\n'.encode('latin-1', 'replace'))
xref = len(pdf)
pdf.extend(f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode())
for offset in offsets[1:]:
    pdf.extend(f'{offset:010d} 00000 n \n'.encode())
pdf.extend(f'trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())

output = Path('output/pdf')
output.mkdir(parents=True, exist_ok=True)
target = output / 'pointnxt-mcp-complete-technical-context.pdf'
target.write_bytes(pdf)
print(target)
