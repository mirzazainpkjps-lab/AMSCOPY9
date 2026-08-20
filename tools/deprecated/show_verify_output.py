p='scripts/verify_output.txt'
with open(p,'rb') as f:
    data=f.read()
try:
    s=data.decode('utf-16')
except Exception:
    try:
        s=data.decode('utf-8')
    except Exception:
        s=str(data)
print(s)
