import importlib.util, pathlib, sqlite3, tempfile, unittest
spec=importlib.util.spec_from_file_location('keeper',pathlib.Path(__file__).with_name('sqlite_read_lifetime.py'));module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
class KeeperTests(unittest.TestCase):
 def test_wal_reader_no_business_writes_or_transaction(self):
  with tempfile.TemporaryDirectory() as t:
   p=pathlib.Path(t)/'db.sqlite';c=sqlite3.connect(p);c.execute('pragma journal_mode=WAL');c.execute('create table facts(id integer)');c.execute('insert into facts values (1)');c.commit();c.close()
   with module.sqlite_read_lifetime(str(p)) as keeper:
    self.assertFalse(keeper.in_transaction)
    with self.assertRaises(sqlite3.OperationalError):keeper.execute('insert into facts values (2)')
    reader=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True);self.assertEqual(reader.execute('select * from facts').fetchall(),[(1,)]);reader.close()
    self.assertTrue(pathlib.Path(str(p)+'-shm').exists())
   c=sqlite3.connect(p);self.assertEqual(c.execute('select * from facts').fetchall(),[(1,)]);c.close()
 def test_missing_database_never_created(self):
  with tempfile.TemporaryDirectory() as t:
   p=pathlib.Path(t)/'absent.sqlite'
   with self.assertRaises(FileNotFoundError):
    with module.sqlite_read_lifetime(str(p)):pass
   self.assertFalse(p.exists())
 def test_memory_database_skipped(self):
  with module.sqlite_read_lifetime(':memory:') as c:self.assertIsNone(c)
if __name__=='__main__':unittest.main()
