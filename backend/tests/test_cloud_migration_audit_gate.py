"""Preview requests must not consume IDs from the live operation log."""
import ast
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import unittest


class AuditGateTest(unittest.TestCase):
    def test_preview_never_persists_audit_but_live_write_still_does(self):
        source = Path(__file__).resolve().parents[1] / 'app/main.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        operation = next(
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == 'operation_audit_middleware'
        )
        operation.decorator_list = []
        rows = []

        class Session:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def add(self, row):
                rows.append(row)

            def commit(self):
                pass

        env = {
            'Request': object,
            'os': os,
            'SessionLocal': Session,
            'OperationLog': lambda **values: values,
            '_is_operation_audit_noise': lambda *_: False,
            '_operation_module': lambda path: '素材库',
            '_operation_action': lambda *_: 'test action',
            'logger': SimpleNamespace(exception=lambda *_: None),
        }
        code = compile(ast.fix_missing_locations(ast.Module(body=[operation], type_ignores=[])), str(source), 'exec')
        exec(code, env)
        middleware = env['operation_audit_middleware']
        request = SimpleNamespace(method='POST', url=SimpleNamespace(path='/api/assets/test'),
                                  state=SimpleNamespace(user={'number': 'test', 'name': 'Test'}))

        async def response(status):
            return SimpleNamespace(status_code=status)

        previous = os.environ.get('CLOUD_MIGRATION_VALIDATION')
        try:
            os.environ['CLOUD_MIGRATION_VALIDATION'] = '1'
            for status in (200, 423):
                result = asyncio.run(middleware(request, lambda _: response(status)))
                self.assertEqual(result.status_code, status)
            self.assertEqual(rows, [])
            os.environ.pop('CLOUD_MIGRATION_VALIDATION')
            result = asyncio.run(middleware(request, lambda _: response(200)))
            self.assertEqual(result.status_code, 200)
            self.assertEqual(len(rows), 1)
        finally:
            if previous is None:
                os.environ.pop('CLOUD_MIGRATION_VALIDATION', None)
            else:
                os.environ['CLOUD_MIGRATION_VALIDATION'] = previous


if __name__ == '__main__':
    unittest.main()
