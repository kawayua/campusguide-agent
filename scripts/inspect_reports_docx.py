import sys
import io
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph


def iter_blocks(doc):
    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag.split('}')[-1]
        if tag == 'p':
            yield Paragraph(child, doc)
        elif tag == 'tbl':
            yield Table(child, doc)


def dump(path, out):
    doc = Document(path)
    out.write("=" * 100 + "\n")
    out.write("FILE: " + path + "\n")
    out.write("=" * 100 + "\n")
    for block in iter_blocks(doc):
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if text:
                out.write("[P|%s] %s\n" % (block.style.name, text))
        else:
            out.write("[TABLE] rows=%d cols=%d\n" % (len(block.rows), len(block.columns)))
            for i, row in enumerate(block.rows):
                cells = [c.text.strip().replace("\n", " / ") for c in row.cells]
                out.write("  R%d | %s\n" % (i, " || ".join(cells)))


if __name__ == "__main__":
    src = sys.argv[1]
    dst = sys.argv[2]
    with io.open(dst, "w", encoding="utf-8") as out:
        dump(src, out)
    print("wrote", dst)