"""Small STEP -> HLR/section -> DXF toolkit. Not an expert drawing planner."""
from pathlib import Path
import hashlib
import json
import math
import cadquery as cq
import ezdxf
from .templates import load_blank_template
from .operations import append as record_operation
from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.gp import gp_Ax2, gp_Pnt, gp_Dir, gp_Pln
from OCP.BRepLib import BRepLib
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.GCPnts import GCPnts_QuasiUniformDeflection
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.BRepPrimAPI import BRepPrimAPI_MakeHalfSpace
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepTools import BRepTools_WireExplorer
from OCP.TopAbs import TopAbs_REVERSED


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def vector(v):
    if len(v)!=3 or not all(math.isfinite(float(x)) for x in v):
        raise ValueError('Expected a finite XYZ vector')
    return cq.Vector(*v)


def basis(normal, xdir):
    n = vector(normal).normalized()
    x = vector(xdir)
    x = (x-n*x.dot(n)).normalized()
    return x, n.cross(x).normalized(), n


def samples(edge, tolerance):
    curve = BRepAdaptor_Curve(edge.wrapped)
    sampler = GCPnts_QuasiUniformDeflection(curve, tolerance)
    if not sampler.IsDone():
        raise RuntimeError('Curve sampling failed')
    return [cq.Vector(sampler.Value(i)) for i in range(1,sampler.NbPoints()+1)]


def wire_polygon(wire, x, y, origin, tolerance):
    exp = BRepTools_WireExplorer(wire.wrapped)
    points = []
    while exp.More():
        edge = cq.Edge(exp.Current())
        pts = samples(edge,tolerance)
        if edge.wrapped.Orientation()==TopAbs_REVERSED:
            pts.reverse()
        for p in pts[:-1]:
            q=p-origin
            points.append((q.dot(x),q.dot(y)))
        exp.Next()
    if len(points)<3:
        raise ValueError('Degenerate section boundary')
    return points


class Drawing:
    def __init__(self, step='input/model.step', path='output/drawing.dxf', title='BenchCAD', template=None):
        self.step, self.path = Path(step), Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.model = cq.importers.importStep(str(self.step)).val()
        template_path = Path(template) if template is not None else Path('input/template.dxf')
        use_template = template is not None or template_path.exists()
        self.doc = load_blank_template(template_path) if use_template else ezdxf.new('R2010')
        self.doc.units = ezdxf.units.MM
        if 'BENCHCAD' not in self.doc.styles:
            self.doc.styles.new('BENCHCAD',dxfattribs={'font':'Arial.ttf'})
        if 'BC_DIM' not in self.doc.dimstyles:
            self.doc.dimstyles.new('BC_DIM',dxfattribs={'dimtxt':3.5,'dimasz':2.5,'dimtxsty':'BENCHCAD','dimclrd':7,'dimclre':7,'dimclrt':7})
        for name,weight in [('FRAME',35),('GEOMETRY',35),('HATCH',18),('DIMENSIONS',25)]:
            if name not in self.doc.layers:
                self.doc.layers.new(name,dxfattribs={'color':7,'lineweight':weight})
        m=self.doc.modelspace()
        if not use_template:
            m.add_lwpolyline([(10,10),(410,10),(410,287),(10,287)],close=True,dxfattribs={'layer':'FRAME'})
            m.add_text(title,dxfattribs={'height':4,'style':'BENCHCAD','layer':'FRAME','insert':(270,18)})
        self.state={'schema':'benchcad-cadquery-working-state/1','source_sha256':digest(self.step),
                    'template_sha256':digest(template_path) if use_template else None,
                    'views':[],'dimensions':[], 'section_evidence':'not_verified_agent_writable'}
        record_operation(self.path,'drawing.started',{'source_sha256':self.state['source_sha256'],
                         'units':'mm','coordinate_frame':'source_step'},reset=True)

    @classmethod
    def open(cls, step='input/model.step', path='output/drawing.dxf'):
        self=cls.__new__(cls)
        self.step,self.path=Path(step),Path(path)
        self.model=cq.importers.importStep(str(self.step)).val()
        self.doc=ezdxf.readfile(self.path)
        self.state=json.loads(self.path.with_suffix('.state.json').read_text(encoding='utf-8'))
        if self.state['source_sha256']!=digest(self.step):
            raise ValueError('Source STEP changed')
        return self

    def _project(self, shape, name, normal, xdir, position, scale):
        if any(v['id']==name for v in self.state['views']):
            raise ValueError('View ID already exists')
        if not math.isfinite(scale) or scale<=0 or not all(math.isfinite(x) for x in position):
            raise ValueError('Invalid view placement')
        x,y,n=basis(normal,xdir)
        hlr=HLRBRep_Algo();hlr.Add(shape.wrapped)
        hlr.Projector(HLRAlgo_Projector(gp_Ax2(gp_Pnt(),gp_Dir(*n.toTuple()),gp_Dir(*x.toTuple()))))
        hlr.Update();hlr.Hide()
        output=HLRBRep_HLRToShape(hlr)
        block=self.doc.blocks.new('VIEW_'+name)
        tolerance=.02/scale
        entities=0
        # Visible edges only. Smooth/seam/hidden-line policy is intentionally
        # limited in this first toolkit and documented in TOOLS.md.
        for method in ('VCompound','Rg1LineVCompound','OutLineVCompound'):
            wrapped=getattr(output,method)()
            if wrapped.IsNull():continue
            BRepLib.BuildCurves3d_s(wrapped,1e-7)
            for edge in cq.Shape.cast(wrapped).Edges():
                curve=BRepAdaptor_Curve(edge.wrapped)
                kind=edge.geomType()
                attrs={'layer':'GEOMETRY'}
                if kind=='LINE':
                    p=curve.Value(curve.FirstParameter());q=curve.Value(curve.LastParameter())
                    block.add_line((p.X(),p.Y()),(q.X(),q.Y()),dxfattribs=attrs)
                elif kind=='CIRCLE' and abs((curve.LastParameter()-curve.FirstParameter())-2*math.pi)<1e-6:
                    c=curve.Circle();p=c.Location()
                    block.add_circle((p.X(),p.Y()),c.Radius(),dxfattribs=attrs)
                else:
                    pts=samples(edge,tolerance)
                    block.add_lwpolyline([(p.x,p.y) for p in pts],dxfattribs=attrs)
                entities+=1
        if entities==0:raise ValueError('Empty projected view')
        insert=self.doc.modelspace().add_blockref(block.name,position,dxfattribs={'xscale':scale,'yscale':scale,'zscale':scale})
        view={'id':name,'type':'projection','normal':n.toTuple(),'xdir':x.toTuple(),
              'position':list(position),'scale':scale,'insert_handle':insert.dxf.handle,
              'block':block.name,'curve_deflection_paper_mm':.02,'projection_scope':'custom_geometry'}
        self.state['views'].append(view)
        return view,block,(x,y,n)

    def projection(self,name,normal=(0,0,1),xdir=(1,0,0),position=(100,180),scale=1):
        view=self._project(self.model,name,normal,xdir,position,scale)[0]
        view['projection_scope']='whole_model'
        record_operation(self.path,'view.created',view)
        return view

    def full_section(self,name,point,normal,xdir=(1,0,0),position=(280,180),scale=1,keep_side=-1):
        if keep_side not in (-1,1):raise ValueError('keep_side must be -1 or 1')
        x,y,n=basis(normal,xdir);origin=vector(point)
        size=max(self.model.BoundingBox().DiagonalLength*4,1)
        face=BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(*origin.toTuple()),gp_Dir(*n.toTuple())),-size,size,-size,size).Face()
        half=BRepPrimAPI_MakeHalfSpace(face,gp_Pnt(*(origin+n*keep_side).toTuple())).Solid()
        cutter=BRepAlgoAPI_Common(self.model.wrapped,half);cutter.Build()
        section=BRepAlgoAPI_Common(self.model.wrapped,face);section.Build()
        if not cutter.IsDone() or not section.IsDone():raise RuntimeError('Section geometry failed')
        remaining=cq.Shape.cast(cutter.Shape());material=cq.Shape.cast(section.Shape())
        faces=material.Faces()
        if not remaining.Solids() or not faces:raise ValueError('Plane does not produce a material section')
        # Viewer is on +normal side; keeping that side would show uncut exterior.
        # Flip viewing direction for keep_side=+1 while preserving x direction.
        viewing=n*(-keep_side)
        view,block,(px,py,pn)=self._project(remaining,name,viewing.toTuple(),xdir,position,scale)
        for f in faces:
            hatch=block.add_hatch(color=7,dxfattribs={'layer':'HATCH'})
            hatch.set_pattern_fill('ANSI31',scale=1/scale)
            outer=f.outerWire()
            for wire in f.Wires():
                pts=wire_polygon(wire,px,py,cq.Vector(0,0,0),.02/scale)
                hatch.paths.add_polyline_path(pts,is_closed=True,flags=1 if wire.isSame(outer) else 0)
        view.update(type='full_section',plane={'point':origin.toTuple(),'normal':n.toTuple()},keep_side=keep_side,
                    section_area_mm2=sum(f.Area() for f in faces),material_faces=len(faces))
        record_operation(self.path,'view.created',view)
        return view

    def linear_dimension(self,p1,p2,base,angle=0,view=None,text='<>'):
        scale=1 if view is None else self.view(view)['scale']
        dim=self.doc.modelspace().add_linear_dim(base=base,p1=p1,p2=p2,angle=angle,
                  text=text,dimstyle='BC_DIM',override={'dimlfac':1/scale},dxfattribs={'layer':'DIMENSIONS'})
        dim.render()
        self.state['dimensions'].append({'handle':dim.dimension.dxf.handle,'view':view,'scale':scale})
        return dim.dimension.dxf.handle

    def view(self,name):
        return next(v for v in self.state['views'] if v['id']==name)

    def delete_view(self,name):
        v=self.view(name)
        if any(d.get('view')==name for d in self.state['dimensions']):
            raise ValueError('Delete dependent dimensions before deleting view')
        self.doc.modelspace().delete_entity(self.doc.entitydb[v['insert_handle']])
        self.doc.blocks.delete_block(v['block'],safe=True)
        self.state['views'].remove(v)
        record_operation(self.path,'view.deleted',{'id':name})

    def note(self,text,position,height=3.5):
        if 'ANNO_TEXT' not in self.doc.layers:self.doc.layers.new('ANNO_TEXT')
        self.doc.modelspace().add_text(text,dxfattribs={'style':'BENCHCAD','height':height,'insert':position,'layer':'ANNO_TEXT'})

    def save(self):
        self.doc.saveas(self.path)
        self.state['dxf_sha256']=digest(self.path)
        self.path.with_suffix('.state.json').write_text(json.dumps(self.state,indent=2),encoding='utf-8')
        record_operation(self.path,'drawing.exported',{'dxf_sha256':self.state['dxf_sha256'],
                         'views':self.state['views']})

    def preview(self,path='preview.png'):
        self.save()
        return render(self.path,path)


def render(dxf,path):
    """Preview model-space geometry, or a single blank mm paper-space sheet."""
    from ezdxf.addons.drawing import Frontend,RenderContext,layout,config
    from ezdxf.addons.drawing.pymupdf import PyMuPdfBackend
    import pymupdf as fitz
    from PIL import Image
    doc=ezdxf.readfile(dxf)
    if not len(doc.modelspace()):
        # Supplied blank templates keep their frame in paper space. Use the
        # same validated conversion as Drawing(), without changing the source.
        doc=load_blank_template(dxf)
    backend=PyMuPdfBackend()
    settings=config.Configuration(background_policy=config.BackgroundPolicy.WHITE,
                                  color_policy=config.ColorPolicy.BLACK)
    Frontend(RenderContext(doc),backend,config=settings).draw_layout(doc.modelspace(),finalize=True)
    pdf=backend.get_pdf_bytes(layout.Page(420,297,layout.Units.mm,margins=layout.Margins.all(5)))
    with fitz.open(stream=pdf,filetype='pdf') as document:
        pix=document[0].get_pixmap(matrix=fitz.Matrix(5,5),alpha=False)
        img=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
        img.thumbnail((2100,1500))
        canvas=Image.new('RGB',(2100,1500),'white')
        canvas.paste(img,((2100-img.width)//2,(1500-img.height)//2))
        canvas.save(path)
    return {'path':str(path),'source_sha256':digest(dxf),'pixels':[2100,1500]}
