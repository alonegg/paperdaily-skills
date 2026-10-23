"""Portable supplement safety behavior in source and assembled packages."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=next(p for p in Path(__file__).resolve().parents if (p/'paperdaily').is_dir())
RUNTIME=ROOT/'_shared' if (ROOT/'_shared').is_dir() else ROOT/'paperdaily'/'lib'
sys.path.insert(0,str(RUNTIME))
import pd_supplement as supplement
from pd_runtime import ApiError, Client, Settings
from pd_supplement_contract import validate_request


class SupplementTests(unittest.TestCase):
    def test_source_symlink_and_fifo_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'file').write_bytes(b'content')
            (root/'link').symlink_to(root/'file')
            with self.assertRaises(ApiError):
                supplement.selected_file(root/'link')
            if hasattr(os,'mkfifo'):
                os.mkfifo(root/'fifo')
                with self.assertRaises(ApiError):
                    supplement.selected_file(root/'fifo')

    def test_lease_file_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'.lease'
            supplement.save_private(path,{'secret':'synthetic'})
            self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_unknown_server_does_not_fallback(self):
        client=SimpleNamespace(get=lambda path:{'paper_supplements':{'enabled':False}})
        with self.assertRaises(ApiError):
            supplement.main(client,['list'])

    def test_source_submission_disabled_before_network(self):
        client=SimpleNamespace(get=lambda path: self.fail('network must not be called'))
        with self.assertRaises(ApiError):
            supplement.main(client,['source-propose'])

    def test_transport_does_not_allow_credential_override(self):
        client=Client(Settings('http://127.0.0.1:1/api/v1','fixture'))
        with self.assertRaises(ApiError):
            client.request('POST','/me/paper-supplements',{},extra_headers={'Authorization':'attacker'})

    def test_contract_rejects_mass_assignment(self):
        self.assertEqual(validate_request('CreateTask',{'paper_id':'W1','baseline_hash':'b'*64,'goals':['methods'],'visibility':'community'}),'schema_invalid')

    def test_prepare_is_not_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'paper.pdf').write_bytes(b'%PDF-fixture')
            task={'id':'a'*32,'paper_id':'W1','baseline_hash':'b'*64,'baseline':{'analysis':{}}}
            (root/'task.json').write_text(json.dumps({'origin':'http://127.0.0.1/api/v1','task':task}))
            client=SimpleNamespace(settings=SimpleNamespace(base='http://127.0.0.1/api/v1'),get=lambda path:{'paper_supplements':{'enabled':True,'schema_versions':['supplement.v1']}})
            with patch.object(supplement,'pdf_pages',return_value=2):
                result=supplement.main(client,['prepare','--dir',str(root),'--source',str(root/'paper.pdf')])
            body=json.loads((root/'supplement.json').read_text())
            self.assertEqual(result['status'],'prepared_not_read')
            self.assertEqual(body['sources'],[])
            self.assertEqual(body['outcome'],'insufficient_material')
            with self.assertRaises(ApiError):
                supplement.validate_local(root)
