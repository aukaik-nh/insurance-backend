import pdfplumber
import sys
sys.stdout.reconfigure(encoding='utf-8')

path = r'C:\Users\Administrator\Desktop\New folder\BabyPreechar\1กก5226 กธ.59.pdf'
with pdfplumber.open(path) as pdf:
    for page in pdf.pages:
        t = page.extract_text()
        if t:
            print(t)
