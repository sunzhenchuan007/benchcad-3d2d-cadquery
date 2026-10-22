"""Replay a FROZEN scoring transform into display meshes; never search or score.

Voxelization and placement reuse the exact evaluator snapshot. A replay that
does not reproduce the saved IoU is rejected instead of changing the score.
"""
from pathlib import Path
import base64,hashlib,json,os,sys


def surface(mask,pitch,centre):
    """Merge exposed coplanar voxel faces without changing any occupied cell."""
    import numpy as np
    quads=[]
    for axis in range(3):
        uv=[i for i in range(3) if i!=axis]
        for sign in [-1,1]:
            neighbour=np.zeros_like(mask);src=[slice(None)]*3;dst=[slice(None)]*3
            src[axis]=slice(1,None) if sign==1 else slice(None,-1)
            dst[axis]=slice(None,-1) if sign==1 else slice(1,None)
            neighbour[tuple(dst)]=mask[tuple(src)];exposed=mask&~neighbour
            for layer in np.flatnonzero(exposed.any(axis=tuple(uv))):
                plane=np.take(exposed,int(layer),axis=axis).copy()
                while plane.any():
                    row,col=np.argwhere(plane)[0];width=height=1
                    while col+width<plane.shape[1] and plane[row,col+width]:width+=1
                    while row+height<plane.shape[0] and plane[row+height,col:col+width].all():height+=1
                    plane[row:row+height,col:col+width]=False
                    q=np.empty((4,3));q[:,axis]=layer+sign*.5
                    q[:,uv[0]]=[row-.5,row+height-.5,row+height-.5,row-.5]
                    q[:,uv[1]]=[col-.5,col-.5,col+width-.5,col+width-.5]
                    quads.append((q-centre)*pitch)
    return np.asarray(quads).reshape(-1,4,3)


def build(reference,candidate,evidence,vendor,output):
    import numpy as np
    import cadquery as cq
    from scipy import ndimage
    # Docker already bounds this trusted replay; no detached meshing subprocess.
    os.environ['BENCHCAD_MESH_GUARD']='0'
    sys.path.insert(0,str(vendor))
    from envs.common import part_metric as pm
    paths=[Path(reference),Path(candidate)];metric=json.loads(Path(evidence).read_text())
    hashes=[hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
    if metric['status']!='computed' or hashes!=[metric['reference']['sha256'],metric['candidate']['sha256']]:raise ValueError('frozen_step_mismatch')
    if metric['grid']!=pm.GRID:raise ValueError('scoring_grid_version_mismatch')
    rotation=np.asarray(metric['selected_transform'])@(np.diag([1,1,-1]) if metric['reflection_used'] else np.eye(3))
    fingerprint=hashlib.sha256(json.dumps([hashes,metric['selected_transform'],metric['reflection_used'],pm.GRID]).encode()).hexdigest()
    output=Path(output);cache=output.with_name('geometry-grid.npz')
    frames={};grids=[]
    if cache.exists():
        with np.load(cache,allow_pickle=False) as z:
            if str(z['fingerprint'])!=fingerprint:raise ValueError('display_cache_identity_mismatch')
            grids=[z['a'].copy(),z['b'].copy()];frames={k:z[k].copy() for k in ['ca','sa','da','cb','sb','db']}
    else:
        for i,path in enumerate(paths):
            shape=cq.importers.importStep(str(path)).val()
            # Mirror the B-rep before tessellation, exactly as the saved scorer did.
            if i and metric['reflection_used']:shape=shape.mirror('XY')
            vertices,faces=pm.tessellate(shape,pm.IOU_DEFLECTION);centre,extent=pm.mesh_frame(vertices)
            block,origin=pm.occupancy(vertices,faces,frame=(centre,extent));idx=pm.world_indices(block,origin)
            if i:idx=pm.rotate_indices(idx,rotation)
            idx,shift=pm.place(idx,'self');grids.append(pm.paste(idx))
            key='b' if i else 'a';frames['c'+key]=centre;frames['s'+key]=extent;frames['d'+key]=shift
        np.savez_compressed(cache,fingerprint=fingerprint,a=grids[0],b=grids[1],**frames)
    a,b=grids;masks=dict(common=a&b,missing=a&~b,extra=b&~a);union=a|b
    counts=dict(common=int(masks['common'].sum()),a_only=int(masks['missing'].sum()),b_only=int(masks['extra'].sum()),union=int(union.sum()))
    replay=counts['common']/counts['union']
    if abs(replay-metric['raw_iou'])>1e-12:raise ValueError('display_replay_score_mismatch')
    centre_index=pm.GRID/2+(pm.GRID_SIZE-pm.GRID)//2;pitch=float(frames['sa'])/pm.GRID
    indices=np.argwhere(union);low=(indices.min(0)-centre_index-.5)*pitch;high=(indices.max(0)-centre_index+.5)*pitch
    labels,_=ndimage.label(masks['missing']|masks['extra']);sizes=np.bincount(labels.ravel());sizes[0]=0
    chosen=np.argwhere(labels==int(np.argmax(sizes))) if sizes.max() else indices
    focus=dict(centre=(((chosen.min(0)+chosen.max(0))*.5-centre_index)*pitch).tolist(),span=max(float((chosen.max(0)-chosen.min(0)+1).max())*pitch*1.5,5))
    enc=lambda x:base64.b64encode(x.tobytes()).decode()
    geometry={};triangles=0
    for key,mask in masks.items():
        quads=surface(mask,pitch,centre_index);positions=quads[:,[0,1,2,0,2,3],:].astype('<f4').reshape(-1)
        geometry[key]=enc(positions);triangles+=len(positions)//9
    surfaces={};ca,sa,cb,sb,da,db=[frames[k] for k in ['ca','sa','cb','sb','da','db']]
    for i,(key,path) in enumerate(zip(['gt','generated'],paths)):
        shape=cq.importers.importStep(str(path)).val()
        if i and metric['reflection_used']:shape=shape.mirror('XY')
        vertices,faces=shape.tessellate(.05,.25);v=np.asarray([p.toTuple() for p in vertices]);faces=np.asarray(faces,dtype='<u4')
        positions=((v-ca)+da/pm.GRID*sa if not i else ((v-cb)*(sa/sb))@rotation.T+db/pm.GRID*sa).astype('<f4')
        surfaces[key]=dict(positions=enc(positions),indices=enc(faces),triangles=len(faces),bounds=[positions.min(0).tolist(),positions.max(0).tolist()])
    data=dict(summary=dict(iou=metric['raw_iou'],grid=pm.GRID,padded_grid=pm.GRID_SIZE,pitch_reference_mm=pitch,counts=counts,reflection_used=metric['reflection_used'],metric_version=metric['metric_version']),geometry=geometry,bounds=[low.tolist(),high.tolist()],focus=focus,triangles=triangles,surfaces=surfaces)
    if [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]!=hashes:raise ValueError('frozen_step_changed')
    output.write_text(json.dumps(data,separators=(',',':'),allow_nan=False),encoding='utf-8')


if __name__=='__main__':build(*sys.argv[1:])
