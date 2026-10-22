import copy
import json
from pathlib import Path
import tempfile
import unittest

from evaluation.drawing_metrics import aggregate, count_penalty, matches, score_expert


def target():
    return {'mode':'contains_axis','axis':{'origin_mm':[0,0,0],'direction':[0,0,1]},
            'position_tolerance_mm':.1,'angle_rule':{'mode':'unrestricted'}}


def expert():
    return {'schema_version':'benchcad-section-position-draft/5','case_id':'case_test',
            'input_sha256':'a'*64,'units':'mm','sections':[{'id':'required','requirement':{'kind':'required'},'target':target()}],
            'view_policy':{'required_groups':[],'reference_schemes':[{'id':'scheme','counts':{'major_views':5,'local_sections':1}}]}}


def candidate():
    return {'case_id':'case_test','input_sha256':'a'*64,'units':'mm','coordinate_frame':'source_step',
            'views':[{'id':'A','kind':'full_section'}],
            'sections':[{'view_id':'A','plane':{'point_mm':[0,0,0],'normal':[1,0,0]}}]}


class Scores(unittest.TestCase):
    def test_product_and_missing(self):
        m={n:{'status':'computed','score':s} for n,s in zip(('reconstruction_iou','expert_preference','annotation_collision'),(.8,.6,.95))}
        self.assertAlmostEqual(aggregate(m)['overall_score'],.456)
        m['reconstruction_iou']['score']=0
        self.assertEqual(aggregate(m)['overall_score'],0)
        m['expert_preference']['status']='not_evaluated'
        self.assertIsNone(aggregate(m)['overall_score'])
    def test_bounds(self):
        m={n:{'status':'computed','score':s} for n,s in zip(('reconstruction_iou','expert_preference','annotation_collision'),(100,1,1))}
        with self.assertRaises(ValueError):aggregate(m)
    def test_counts_and_allowance(self):
        for major,local,excess in [(5,1,0),(4,2,0),(5,2,0),(5,3,1),(6,1,1)]:
            self.assertEqual(count_penalty(major,local,{'major_views':5,'local_sections':1})['excess'],excess)
        with self.assertRaises(ValueError):count_penalty(0,0,{'major_views':0,'local_sections':0})
    def test_contains_vs_crosses(self):
        p={'point_mm':[0,0,0],'normal':[1,0,0]}
        self.assertTrue(matches(p,target()))
        self.assertFalse(matches({'point_mm':[0,0,0],'normal':[0,0,1]},target()))
        t=target();t.update(mode='crosses_axis_at_station',station_mm=5)
        self.assertTrue(matches({'point_mm':[0,0,5],'normal':[0,0,-1]},t))
        self.assertFalse(matches(p,t))
        self.assertFalse(matches({'point_mm':[0,0,6],'normal':[0,0,1]},t))
    def test_two_lines_and_angle(self):
        t=target();t['mode']='contains_lines';t['lines']=[t['axis'],{'origin_mm':[0,3,0],'direction':[0,1,0]}]
        p={'point_mm':[0,0,0],'normal':[1,0,0]}
        self.assertTrue(matches(p,t))
        t['angle_rule']={'mode':'plane_normal','normal':[0,1,0],'tolerance_deg':2}
        self.assertFalse(matches(p,t))
    def test_required_and_group_not_each_alternative(self):
        c=candidate();g=expert()
        self.assertEqual(score_expert(c,g,evidence_verified=True)['score'],1)
        c['sections']=[]
        self.assertEqual(score_expert(c,g,evidence_verified=True)['score'],.8)
        g['view_policy']['required_groups']=[{'id':'group'}]
        for i in range(2):
            g['sections'].append({'id':str(i),'requirement':{'kind':'one_of','group_id':'group'},'target':target()})
        result=score_expert(c,g,evidence_verified=True)
        self.assertEqual(len(result['missing_requirements']),2)
        self.assertAlmostEqual(result['score'],.6)
    def test_unsupported_evidence_never_full_score(self):
        self.assertIsNone(score_expert(candidate(),expert())['score'])
        c=candidate();c['input_sha256']='b'*64
        with self.assertRaises(ValueError):score_expert(c,expert(),evidence_verified=True)
    def test_combined_count_and_required(self):
        c=candidate();c['views']=[{'id':str(i),'kind':'projection' if i<5 else 'detail'} for i in range(8)];c['sections']=[]
        self.assertAlmostEqual(score_expert(c,expert(),evidence_verified=True)['score'],1-1/6-.2)
    def test_floor(self):
        c=candidate();c['sections']=[];g=expert()
        g['sections']=[{'id':str(i),'requirement':{'kind':'required'},'target':target()} for i in range(8)]
        self.assertEqual(score_expert(c,g,evidence_verified=True)['score'],0)


class CollisionTests(unittest.TestCase):
    def setUp(self):
        try:import ezdxf
        except ImportError:self.skipTest('CAD dependencies unavailable')
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'drawing.dxf';self.doc=ezdxf.new('R2010');self.doc.units=4;self.m=self.doc.modelspace()
    def score(self):
        from evaluation.collisions import score_dxf
        self.doc.saveas(self.path);return score_dxf(self.path)
    def text(self,content='TEXT',position=(0,0),rotation=0):
        self.m.add_text(content,dxfattribs={'height':3,'insert':position,'rotation':rotation})
    def test_text_pair_and_annotation_line_contacts(self):
        self.text();self.text(position=(.5,0));self.doc.layers.new('ANNO_DIM');self.m.add_line((-10,1),(20,1),dxfattribs={'layer':'ANNO_DIM'})
        r=self.score();self.assertEqual(r['pair_count'],3);self.assertEqual(r['score'],.85)
    def test_separated_rotated_text(self):
        self.text(rotation=90);self.text(position=(20,0),rotation=90)
        self.assertEqual(self.score()['pair_count'],0)
    def test_dimension_line_crossing_another_text(self):
        self.m.add_linear_dim(base=(0,5),p1=(0,0),p2=(20,0)).render()
        self.text('X',position=(1,4))
        self.assertEqual(self.score()['counts']['text_annotation'],1)
    def test_twenty_pairs_floor(self):
        for _ in range(8):self.text()
        self.assertEqual(self.score()['pair_count'],28)
        self.assertEqual(self.score()['score'],0)
    def test_view_and_frame_text_are_both_checked(self):
        self.doc.layers.new('FRAME');self.doc.blocks.new('VIEW_A').add_text('TEXT',dxfattribs={'height':3})
        self.m.add_blockref('VIEW_A',(0,0));self.text()
        self.m.add_text('TEXT',dxfattribs={'height':3,'layer':'FRAME'})
        self.assertEqual(self.score()['pair_count'],3)
    def test_normal_dimension_gap_has_no_contact(self):
        self.m.add_linear_dim(base=(0,5),p1=(0,0),p2=(10,0)).render()
        self.assertEqual(self.score()['pair_count'],0)
    def test_two_dimensions_count_pair_once(self):
        for _ in range(2):self.m.add_linear_dim(base=(0,5),p1=(0,0),p2=(10,0)).render()
        self.assertEqual(self.score()['pair_count'],1)
    def test_leader_without_text_has_no_penalty(self):
        self.m.add_leader([(0,0),(1,1),(2,1)])
        self.assertEqual(self.score()['score'],1)
    def test_annotation_line_without_text_has_no_penalty(self):
        self.doc.layers.new('ANNO_DIM')
        self.m.add_line((0,0),(10,0),dxfattribs={'layer':'ANNO_DIM'})
        self.assertEqual(self.score()['score'],1)

    def test_view_edges_deduplicate_and_annotation_contact_counts(self):
        block=self.doc.blocks.new('VIEW_A')
        for y in (.5,1.,1.5):block.add_line((-2,y),(20,y))
        self.m.add_blockref('VIEW_A',(0,0))
        self.text('TEXT')
        self.doc.layers.new('ANNO_SECTION')
        self.m.add_line((-3,1),(30,1),dxfattribs={'layer':'ANNO_SECTION'})
        r=self.score()
        self.assertEqual(r['counts'],{'text_text':0,'text_view':1,'text_annotation':1,'text_geometry':0})
        self.assertEqual(r['score'],.9)

    def test_text_in_empty_view_interior_is_not_collision(self):
        b=self.doc.blocks.new('VIEW_A')
        b.add_lwpolyline([(-10,-10),(30,-10),(30,20),(-10,20)],close=True)
        self.m.add_blockref('VIEW_A',(0,0));self.text()
        self.assertEqual(self.score()['pair_count'],0)

    def test_check_copy_red_and_original_unchanged(self):
        from evaluation.collisions import score_dxf
        import ezdxf
        self.text();self.text(position=(.5,0));self.doc.saveas(self.path)
        original=self.path.read_bytes();check=self.path.with_name('check.dxf')
        r=score_dxf(self.path,check_path=check)
        self.assertEqual(self.path.read_bytes(),original)
        marked=[e for e in ezdxf.readfile(check).modelspace() if e.dxf.layer=='BC_COLLISION_CHECK']
        self.assertEqual(len(marked),2)
        self.assertTrue(all(e.dxf.color==1 for e in marked))
        with self.assertRaises(ValueError):score_dxf(self.path,check_path=self.path)
        with self.assertRaises(ValueError):score_dxf(self.path,check_path=check)

    def test_unknown_line_role_still_checked(self):
        self.text();self.m.add_line((-5,1),(20,1))
        self.assertEqual(self.score()['counts']['text_geometry'],1)

    def test_empty_attributes_not_missing_text(self):
        self.m.add_text('',dxfattribs={'height':3})
        self.assertEqual(self.score()['score'],1)

    def test_original_red_centerline_is_black_in_check_copy(self):
        from evaluation.collisions import score_dxf
        import ezdxf
        self.doc.layers.new('GEOM_CENTER',dxfattribs={'color':1})
        center=self.m.add_line((30,-5),(30,20),dxfattribs={'layer':'GEOM_CENTER'})
        center.dxf.true_color=0xFF0000
        block=self.doc.blocks.new('VIEW_COLOR_TEST')
        edge=block.add_line((40,0),(40,20),dxfattribs={'color':1})
        edge.dxf.true_color=0xFF0000
        self.m.add_blockref('VIEW_COLOR_TEST',(0,0))
        self.text();self.text(position=(.5,0));self.doc.saveas(self.path)
        before=self.path.read_bytes();check=self.path.with_name('monochrome-check.dxf')
        result=score_dxf(self.path,check_path=check)
        reopened=ezdxf.readfile(check)
        for handle in (center.dxf.handle,edge.dxf.handle):
            entity=reopened.entitydb[handle]
            self.assertEqual(entity.dxf.color,7)
            self.assertFalse(entity.dxf.hasattr('true_color'))
        marks=[e for e in reopened.modelspace() if e.dxf.layer=='BC_COLLISION_CHECK']
        self.assertTrue(marks)
        self.assertTrue(all(e.dxf.color==1 for e in marks))
        self.assertEqual(result['counts'],{'text_text':1,'text_view':0,'text_annotation':0,'text_geometry':0})
        self.assertEqual(self.path.read_bytes(),before)


class GeometryEvidence(unittest.TestCase):
    def test_logged_view_verified_then_tamper_rejected(self):
        try:import cadquery as cq
        except ImportError:self.skipTest('CAD dependencies unavailable')
        from benchcad_cadquery.drawing import Drawing
        from evaluation.view_evidence import verify
        import ezdxf,hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);step=root/'input.step';path=root/'drawing.dxf'
            cq.exporters.export(cq.Workplane('XY').box(20,16,10),str(step))
            d=Drawing(step,path);d.projection('TOP');d.full_section('A',[0,0,0],[0,1,0]);d.save()
            log=path.with_suffix('.operations.jsonl')
            result=verify(path,step,log,case_id='case_test')
            self.assertEqual(len(result['views']),2);self.assertEqual(len(result['sections']),1)
            doc=ezdxf.readfile(path);doc.blocks.get('VIEW_A').add_line((0,0),(5,5));doc.saveas(path)
            events=[json.loads(s) for s in log.read_text().splitlines()];events[-1]['payload']['dxf_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
            log.write_text('\n'.join(json.dumps(e) for e in events))
            with self.assertRaisesRegex(ValueError,'geometry differs'):verify(path,step,log,case_id='case_test')


if __name__=='__main__':unittest.main()
