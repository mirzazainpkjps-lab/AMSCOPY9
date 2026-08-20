import sys
p = sys.argv[1] if len(sys.argv)>1 else 'scripts/verify_refund_full_output.txt'
out = p + '.dec'
with open(p,'rb') as f:
    data = f.read()
for enc in ('utf-16','utf-8','latin1'):
    try:
        s = data.decode(enc)
        with open(out,'w',encoding='utf-8') as fo:
            fo.write(s)
        print('decoded with',enc,'->',out)
        break
    except Exception as e:
        #print('fail',enc,e)
        pass
