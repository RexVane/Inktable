"""冻结环境下的嵌入自检 —— 加入 /health"""
import sys, os
from pathlib import Path
def model_dir():
    if getattr(sys,"frozen",False):
        return Path(sys._MEIPASS)/"embedding_model"
    return Path(__file__).resolve().parent/"embedding_model"
try:
    import numpy as np
    from model2vec import StaticModel
    d=model_dir()
    m=StaticModel.from_pretrained(str(d))
    v=m.encode(["汝窑天青釉","Sn58Bi 钎料"])
    v=v/np.linalg.norm(v,axis=1,keepdims=True)
    print({"frozen":getattr(sys,"frozen",False),"dir":str(d),"dim":m.dim,
           "sim":round(float(v[0]@v[1]),3),"ok":True})
except Exception as e:
    print({"ok":False,"error":f"{type(e).__name__}: {e}"})
