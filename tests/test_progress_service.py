from pathlib import Path
import importlib.util,json,sys,tempfile,unittest
from unittest.mock import patch
from benchcad_runtime.progress import project,validate,utc
from benchcad_runtime.workbench import base_run,dump,sha,read

class ProgressTests(unittest.TestCase):
    def test_score_overrides_stale_controller_failure(self):
        row=project(6,'trial','Case 06',heartbeat={'status':'stopped','controller_pid':123,'raw_log':'private'},metric={'status':'computed','reconstruction_score_100':100},publication='published')
        self.assertEqual(row['stage'],'completed');self.assertEqual(row['status'],'completed')
        self.assertNotIn('controller_pid',row);self.assertNotIn('raw_log',row)
    def test_heartbeat_supplies_running_stage(self):
        row=project(6,'trial','Case 06',heartbeat={'status':'running','stage':'drawing'})
        self.assertEqual(row['stage'],'drawing')
    def test_reconstruction_waiting_for_score_is_not_complete(self):
        row=project(6,'trial','Case 06',drawing={'submitted':True},reconstruction={'submitted':True})
        self.assertEqual(row['stage'],'evaluation');self.assertIsNone(row['score'])
    def test_public_payload_rejects_private_information(self):
        with self.assertRaises(ValueError):project(6,'trial','C:/Users/example')
        row=project(6,'trial','Case 06');row['pid']=123
        with self.assertRaises(ValueError):validate(dict(schema='benchcad-live-progress/1',updated_at=utc(),interval_seconds=60,runs=[row]))

class WorkerTests(unittest.TestCase):
    def setUp(self):
        path=Path(__file__).resolve().parents[2]/'benchmark-architecture/progress_service.py'
        if not path.exists():self.skipTest('workbench checkout absent')
        sys.path.insert(0,str(path.parent));spec=importlib.util.spec_from_file_location('progress_service_test',path)
        self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.d=self.root/'drawing';self.r=self.root/'reconstruction';self.site=self.root/'site';self.work=self.root/'worker'
        self.site.mkdir();(self.site/'cadquery-runs').mkdir()
        (self.site/'cadquery-runs/catalog.json').write_bytes(dump(dict(schema='benchcad-workbench-catalog/1',benchmark='BenchCAD 3Dto2D CADQuery',default_case=6,default_run='trial',runs=[],cases=[])))
        from PIL import Image
        import io
        img=io.BytesIO();Image.new('RGB',(20,20),'white').save(img,format='PNG')
        def bundle(base,stage,files):
            folder=base/stage/'bundle';folder.mkdir(parents=True)
            for name,data in files.items():(folder/name).write_bytes(data)
            (folder/'manifest.json').write_bytes(dump({'files':{name:{'sha256':sha(data),'bytes':len(data)} for name,data in files.items()}}))
        bundle(self.d,'drawing',{'drawing.png':img.getvalue(),'drawing.dxf':b'0\nEOF\n'})
        bundle(self.r,'reconstruction',{'model.step':b'candidate'})
        for base in [self.d,self.r]:(base/'bridge-summary.json').write_bytes(dump(dict(submitted=True,cad_executions=2,wall_seconds=10)))
        ev=self.r/'evaluation';(ev/'workspace').mkdir(parents=True);(ev/'workspace/reference.step').write_bytes(b'reference')
        metric=dict(status='computed',metric_version='normalized-iou48-primitive-threshold/2',raw_iou=1.,iou24=1.,primitive_baseline=.2,corrected_iou=1.,reconstruction_score_100=100.,reflection_used=False,reference={'sha256':sha(b'reference'),'extents_mm':[1,1,1]},candidate={'sha256':sha(b'candidate'),'extents_mm':[1,1,1]},absolute_size_diagnostics={'volume_ratio':1})
        (ev/'candidate.json').write_bytes(dump(metric));(ev/'identity.json').write_bytes(dump({'raw_iou':1}))
        (self.r/'summary.json').write_bytes(dump({'success':True,'evaluation_seconds':5}))
        self.entry=dict(case_number=6,run_id='trial',title='Case 06',drawing_run=str(self.d),reconstruction_run=str(self.r),public_run=base_run(6,'trial','Fixture',model='fixture'))
        self.registry=self.root/'registry.json';self.registry.write_bytes(dump({'runs':[self.entry]}))
        self.config=dict(site_current=str(self.site),work_dir=str(self.work),registry=str(self.registry),render_previews=False)
    def test_completion_builds_report_and_catalog_without_model(self):
        payload=self.module.tick(self.config)
        self.assertEqual(payload['runs'][0]['publication'],'published')
        self.assertTrue((self.site/'cadquery-runs/case-006/trial/report.html').exists())
        catalog=read(self.site/'cadquery-live/catalog.json');self.assertEqual(len(catalog['runs']),1)
        # A second pass does not regenerate the frozen report/package.
        with patch.object(self.module,'report_package',side_effect=AssertionError('must not regenerate')):self.module.tick(self.config)
    def test_publication_failure_retries_cached_package_only(self):
        original=self.module.publish_package
        with patch.object(self.module,'publish_package',side_effect=OSError('private detail')):
            payload=self.module.tick(self.config)
        self.assertEqual(payload['runs'][0]['publication'],'retry')
        package=self.work/'packages/case-006--trial';self.assertTrue(package.exists())
        before=(package/'manifest.json').read_bytes()
        with patch.object(self.module,'report_package',wraps=self.module.report_package):self.module.tick(self.config)
        self.assertEqual(before,(package/'manifest.json').read_bytes())
        self.assertEqual(read(self.site/'cadquery-live/status.json')['runs'][0]['publication'],'published')
        self.assertNotIn('private detail',(self.site/'cadquery-live/status.json').read_text())
    def test_frozen_artifact_tampering_blocks_publication(self):
        (self.r/'reconstruction/bundle/model.step').write_bytes(b'changed')
        payload=self.module.tick(self.config)
        self.assertEqual(payload['runs'][0]['publication'],'retry')
        self.assertFalse((self.site/'cadquery-live/catalog.json').exists())
    def test_site_release_change_republishes_without_model(self):
        self.module.tick(self.config)
        new=self.root/'new-site';(new/'cadquery-runs').mkdir(parents=True)
        (new/'cadquery-runs/catalog.json').write_bytes((self.site/'cadquery-runs/catalog.json').read_bytes())
        self.config['site_current']=str(new)
        with patch.object(self.module,'report_package',side_effect=AssertionError('must use cached package')):self.module.tick(self.config)
        self.assertTrue((new/'cadquery-runs/case-006/trial/report.html').exists())
    def test_trusted_site_hardlinks_are_supported(self):
        ref=self.site/'references';ref.mkdir();(ref/'catalog.json').write_bytes(dump([]))
        import os
        os.link(ref/'catalog.json',self.root/'old-reference-catalog.json')
        self.assertEqual(self.module.tick(self.config)['runs'][0]['publication'],'published')
    def test_separate_successful_scoring_attempt(self):
        import shutil
        evaluation=self.root/'score-attempt';evaluation.mkdir()
        shutil.move(str(self.r/'evaluation'),str(evaluation/'evaluation'))
        shutil.move(str(self.r/'summary.json'),str(evaluation/'summary.json'))
        self.entry['evaluation_run']=str(evaluation);self.registry.write_bytes(dump({'runs':[self.entry]}))
        self.assertEqual(self.module.tick(self.config)['runs'][0]['publication'],'published')
    def test_waits_for_final_usage_handoff(self):
        self.entry['report_ready']=False;self.registry.write_bytes(dump({'runs':[self.entry]}))
        with patch.object(self.module,'report_package',side_effect=AssertionError('metadata is not ready')):self.module.tick(self.config)
        self.assertFalse((self.site/'cadquery-live/catalog.json').exists())
        self.entry['report_ready']=True;self.registry.write_bytes(dump({'runs':[self.entry]}))
        self.assertEqual(self.module.tick(self.config)['runs'][0]['publication'],'published')
    def test_controller_registers_task_automatically(self):
        from types import SimpleNamespace
        from benchcad_cadquery.cli import register_live_task
        target=self.root/'new-run';target.mkdir()
        args=SimpleNamespace(progress_registry=self.root/'new-registry.json',run_id='new-task',case_number=8,model='chosen-model',case_title='Case 08',reasoning_effort='high')
        register_live_task(target,args)
        row=read(args.progress_registry)['runs'][0]
        self.assertEqual(row['case_number'],8);self.assertFalse(row['report_ready'])
        self.assertEqual(read(target/'workbench-run-id.json')['id'],'new-task')
    def test_failed_scoring_still_publishes_unscored_report(self):
        (self.r/'evaluation/candidate.json').write_bytes(dump({'status':'failed','score':None}))
        payload=self.module.tick(self.config);row=payload['runs'][0]
        self.assertEqual(row['status'],'failed');self.assertEqual(row['publication'],'published');self.assertIsNone(row['score'])
        result=read(self.site/'cadquery-live/catalog.json')['runs'][0]
        self.assertIsNone(result['metrics']['raw_iou']);self.assertIsNone(result['metrics']['reconstruction_score_100'])

    def geometry(self):
        import base64,struct
        positions=base64.b64encode(struct.pack('<9f',0,0,0,1,0,0,0,1,0)).decode()
        indices=base64.b64encode(struct.pack('<3I',0,1,2)).decode()
        mesh=dict(positions=positions,indices=indices,triangles=1,bounds=[[0,0,0],[1,1,0]])
        data=dict(summary=dict(iou=1.,grid=128,padded_grid=133,pitch_reference_mm=1,counts=dict(common=1,a_only=0,b_only=0,union=1),reflection_used=False,metric_version='normalized-iou48-primitive-threshold/2'),geometry=dict(common=positions,missing='',extra=''),bounds=[[0,0,0],[1,1,1]],focus=dict(centre=[0,0,0],span=1),triangles=1,surfaces=dict(gt=mesh,generated=mesh))
        path=self.root/'geometry.json';path.write_bytes(dump(data));return path

    def test_geometry_supplement_adds_viewer_and_survives_new_release(self):
        self.module.tick(self.config);package=self.work/'packages/case-006--trial'
        before=(package/'manifest.json').read_bytes()
        self.module.publish_package(package,self.work,self.site,geometry=self.geometry())
        run=read(self.site/'cadquery-live/catalog.json')['runs'][0]
        self.assertTrue((self.site/run['diff_viewer']).exists())
        self.assertIn('viewer.html',(self.site/run['report_file']).read_text(encoding='utf-8'))
        self.assertEqual(before,(package/'manifest.json').read_bytes())
        new=self.root/'new-site';(new/'cadquery-runs').mkdir(parents=True)
        (new/'cadquery-runs/catalog.json').write_bytes((self.site/'cadquery-runs/catalog.json').read_bytes())
        self.config['site_current']=str(new);self.module.tick(self.config)
        restored=read(new/'cadquery-live/catalog.json')['runs'][0]
        self.assertEqual(restored['diff_viewer'],run['diff_viewer'])
        self.assertTrue((new/restored['diff_viewer']).exists())
        self.assertEqual(restored['metrics']['raw_iou'],1.)

    def test_geometry_with_different_score_cannot_be_published(self):
        self.module.tick(self.config);path=self.geometry();data=read(path)
        data['summary']['iou']=.5;path.write_bytes(dump(data))
        with self.assertRaisesRegex(ValueError,'geometry_score_mismatch'):
            self.module.publish_package(self.work/'packages/case-006--trial',self.work,self.site,geometry=path)
        self.assertIsNone(read(self.site/'cadquery-live/catalog.json')['runs'][0]['diff_viewer'])

    def test_geometry_failure_preserves_completed_report_and_retries_cache_only(self):
        self.module.tick(self.config);self.config['render_previews']=True
        state_path=self.work/'worker-state.json';state=read(state_path)
        state['6--trial']['comparison_ready']=True;state_path.write_bytes(dump(state))
        with patch.object(self.module,'preview_for',side_effect=RuntimeError('display failure')):
            self.module.tick(self.config)
        self.assertEqual(read(self.site/'cadquery-live/status.json')['runs'][0]['publication'],'published')
        with patch.object(self.module,'preview_for',side_effect=AssertionError('respect retry backoff')):
            self.module.tick(self.config)
        state=read(state_path);state['6--trial']['geometry_retry_at']=0;state_path.write_bytes(dump(state))
        with patch.object(self.module,'report_package',side_effect=AssertionError('no rerun')):
            with patch.object(self.module,'preview_for',return_value=self.geometry()):self.module.tick(self.config)
        self.assertTrue(read(state_path)['6--trial']['geometry_ready'])
        self.assertTrue(read(self.site/'cadquery-live/catalog.json')['runs'][0]['diff_viewer'])

if __name__=='__main__':unittest.main()
