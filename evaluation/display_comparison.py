"""Offline surface preview in the trusted evaluator container; no model calls."""
from pathlib import Path
import hashlib,json,math,sys

def render(reference,candidate,evidence,output):
    import cadquery as cq
    import numpy as np
    from PIL import Image,ImageDraw
    metric=json.loads(Path(evidence).read_text())
    image=Image.new('RGB',(1400,650),'white');draw=ImageDraw.Draw(image)
    view=np.array([[.7071,-.7071,0],[.4082,.4082,.8165],[-.5774,-.5774,.5774]])
    for i,path in enumerate([reference,candidate]):
        data=Path(path).read_bytes()
        if hashlib.sha256(data).hexdigest()!=metric['candidate' if i else 'reference']['sha256']:raise ValueError('Frozen STEP hash mismatch')
        shape=cq.importers.importStep(str(path)).val();v,f=shape.tessellate(.15,.25)
        v=np.asarray([p.toTuple() for p in v]);f=np.asarray(f);v-=(v.min(0)+v.max(0))/2;v/=float(np.ptp(v,axis=0).max())
        if i:v=v@np.asarray(metric['selected_transform']).T
        projected=v@view.T;tri=projected[f]
        for index in np.argsort(tri[:,:,2].mean(1)):
            t=tri[index];normal=np.cross(t[1]-t[0],t[2]-t[0]);norm=float(np.linalg.norm(normal));brightness=.48+.48*(abs(normal[2])/norm if norm else 0)
            color=tuple(int(c*brightness) for c in (167,196,220))
            xy=[(int(i*700+350+p[0]*480),int(330-p[1]*480)) for p in t]
            draw.polygon(xy,fill=color)
        draw.text((i*700+230,25),'AI reconstructed STEP' if i else 'Original STEP',fill='#34455a')
    draw.text((210,615),'Cached surfaces; centered, normalized and aligned using the frozen scoring transform.',fill='#6a7787')
    image.save(output)
if __name__=='__main__':render(*sys.argv[1:])
