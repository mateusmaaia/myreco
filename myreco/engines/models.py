# MIT License

# Copyright (c) 2016 Diogo Dutra

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.


from falconswagger.models.base import get_model_schema
from falconswagger.exceptions import ModelBaseError
from myreco.engines.types.base import EngineTypeChooser
from myreco.engines.types.items_indices_map import ItemsIndicesMap
from myreco.items_types.models import build_item_key
from types import MethodType, FunctionType
from jsonschema import ValidationError
from sqlalchemy.ext.declarative import AbstractConcreteBase, declared_attr
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from falcon import HTTPNotFound
import sqlalchemy as sa
import json
import random
import logging


class EnginesModelBase(AbstractConcreteBase):
    __tablename__ = 'engines'
    __schema__ = get_model_schema(__file__)
    _jobs = dict()

    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    name = sa.Column(sa.String(255), unique=True, nullable=False)
    configuration_json = sa.Column(sa.Text, nullable=False)

    @property
    def configuration(self):
        if not hasattr(self, '_configuration'):
            self._configuration = json.loads(self.configuration_json)
        return self._configuration

    @declared_attr
    def store_id(cls):
        return sa.Column(sa.ForeignKey('stores.id'), nullable=False)

    @declared_attr
    def type_name_id(cls):
        return sa.Column(sa.ForeignKey('engines_types_names.id'), nullable=False)

    @declared_attr
    def item_type_id(cls):
        return sa.Column(sa.ForeignKey('items_types.id'), nullable=False)

    @declared_attr
    def type_name(cls):
        return sa.orm.relationship('EnginesTypesNamesModel')

    @declared_attr
    def item_type(cls):
        return sa.orm.relationship('ItemsTypesModel')

    @declared_attr
    def store(cls):
        return sa.orm.relationship('StoresModel')

    @property
    def type_(self):
        if not hasattr(self, '_type'):
            self._set_type()
        return self._type

    def _set_type(self):
        todict_schema = {'variables': False}
        self._type = EngineTypeChooser(self.type_name.name)(self.todict(todict_schema))

    def _setattr(self, attr_name, value, session, input_):
        if attr_name == 'configuration':
            value = json.dumps(value)
            attr_name = 'configuration_json'

        if attr_name == 'type_name_id':
            value = {'id': value}
            attr_name = 'type_name'

        if attr_name == 'item_type_id':
            value = {'id': value}
            attr_name = 'item_type'

        super()._setattr(attr_name, value, session, input_)

    def _format_output_json(self, dict_inst, schema):
        if schema.get('configuration') is not False:
            dict_inst.pop('configuration_json')
            dict_inst['configuration'] = self.configuration

        if schema.get('variables') is not False:
            dict_inst['variables'] = self.type_.get_variables()


class EnginesModelDataImporterBase(EnginesModelBase):
    __schema__ = get_model_schema(__file__, 'data_importer_schema.json')

    @classmethod
    def post_import_data(cls, req, resp):
        engine, session = cls._get_engine(req, resp, todict=False)
        data_importer = import_module(engine.configuration['data_importer_path'])
        job_hash = '{:x}'.format(random.getrandbits(128))
        items_indices_map = cls._build_items_indices_map(session, engine)

        cls._background_run(data_importer.get_data, job_hash, session, engine, items_indices_map)
        resp.body = json.dumps({'hash': job_hash})

    @classmethod
    def _get_engine(cls, req, resp, todict=True):
        id_ = req.context['parameters']['uri_template']['id']
        session = req.context['session']
        session = type(session)(bind=session.bind, redis_bind=session.redis_bind)
        engines = cls.get(session, {'id': id_}, todict=todict)

        if not engines:
            raise HTTPNotFound()

        return engines[0], session

    @classmethod
    def _build_items_indices_map(cls, session, engine):
        item_collection_model = cls.__api__.models[build_item_key(engine.item_type.name)]
        item_model = item_collection_model.__models__[engine.store_id]
        return ItemsIndicesMap(session, item_model)

    @classmethod
    def _background_run(cls, func_, job_hash, session, *args, **kwargs):
        executor = ThreadPoolExecutor(2)
        job = executor.submit(func_, *args, **kwargs)
        executor.submit(cls._job_watcher, executor, job, job_hash, session)

    @classmethod
    def _job_watcher(cls, executor, job, job_hash, session):
        cls._jobs[job_hash] = {'status': 'running'}
        try:
            result = job.result()
        except Exception as error:
            result = {'name': error.__class__.__name__, 'message': str(error)}
            cls._jobs[job_hash] = {'status': 'error', 'result': result}
            logging.exception(error)
        else:
            cls._jobs[job_hash] = {'status': 'done', 'result': result}
        finally:
            session.close()

        executor.shutdown()

    @classmethod
    def get_import_data(cls, req, resp):
        cls._get_job(req, resp)

    @classmethod
    def _get_job(cls, req, resp):
        status = cls._jobs.get(req.context['parameters']['query_string']['hash'])

        if status is None:
            raise HTTPNotFound()

        resp.body = json.dumps(status)


class EnginesModelObjectsExporterBase(EnginesModelDataImporterBase):
    __schema__ = get_model_schema(__file__, 'objects_exporter_schema.json')

    @classmethod
    def post_export_objects(cls, req, resp):
        import_data = req.context['parameters']['query_string'].get('import_data')
        job_hash = '{:x}'.format(random.getrandbits(128))
        engine, session = cls._get_engine(req, resp, todict=False)
        items_indices_map = cls._build_items_indices_map(session, engine)
        items_indices_map.update()

        if import_data:
            cls._background_run(cls._run_import_export, job_hash, session,
                                engine, session, items_indices_map)
        else:
            cls._background_run(engine.type_.export_objects, job_hash,
                                session, session, items_indices_map)

        resp.body = json.dumps({'hash': job_hash})

    @classmethod
    def _run_import_export(cls, engine, session, items_indices_map):
        data_importer = import_module(engine.configuration['data_importer_path'])
        data_importer.get_data(engine, items_indices_map)
        return engine.type_.export_objects(session, items_indices_map)

    @classmethod
    def get_export_objects(cls, req, resp):
        cls._get_job(req, resp)


class EnginesTypesNamesModelBase(AbstractConcreteBase):
    __tablename__ = 'engines_types_names'
    __use_redis__ = False

    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String(255), unique=True, nullable=False)
