with open(r'C:\Users\Administrator\Desktop\New folder\Baby78\Dbconfig.INI', 'rb') as f:
    raw = f.read()

idx = raw.find(b'Envelope=')
if idx >= 0:
    line_end = raw.find(b'\r\n', idx)
    envelope_bytes = raw[idx+9:line_end]
    print(f'Envelope hex: {envelope_bytes.hex()}')
    print(f'Envelope len: {len(envelope_bytes)}')
    print(f'As cp874:  {envelope_bytes.decode("cp874",   errors="replace")}')
    print(f'As cp1252: {envelope_bytes.decode("cp1252",  errors="replace")}')
    print(f'As ascii:  {envelope_bytes.decode("ascii",   errors="replace")}')

# also try vcx=
idx2 = raw.find(b'vcx=')
if idx2 >= 0:
    line_end2 = raw.find(b'\r\n', idx2)
    vcx = raw[idx2+4:line_end2]
    print(f'vcx: {vcx}')

# ลอง decode Baby78_Safety.mdb password ด้วย Envelope key
print()
NULL_KEY = bytes.fromhex('86fbec375d449cfac65e28e613b68a6054947bc2')
with open(r'C:\Users\Administrator\Desktop\New folder\Baby78\Baby78_Safety.mdb', 'rb') as f:
    data = f.read(256)
stored = bytes(data[0x42:0x56])
pw_bytes = bytes([a ^ b for a, b in zip(stored, NULL_KEY)])
print(f'Baby78_Safety decoded PW bytes: {pw_bytes.hex()}')
# try trimming to envelope key length
pw_short = pw_bytes[:len(envelope_bytes)].rstrip(b'\x00')
print(f'First {len(envelope_bytes)} bytes: {pw_short.hex()}')
print(f'XOR with Envelope: {bytes([a^b for a,b in zip(pw_short, envelope_bytes)]).hex()}')
