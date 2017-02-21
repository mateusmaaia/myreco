# MIT License

# Copyright (c) 2016 Diogo Dutra <dutradda@gmail.com>

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


from myreco.items_types.items.models_collection import ItemsModelsCollection
from myreco.items_types.base_model import ItemsTypesModelBase
from myreco.utils import ModuleObjectLoader
from swaggerit.exceptions import SwaggerItModelError
from jsonschema import Draft4Validator
from tempfile import NamedTemporaryFile
from aiofiles import zip_open, open as aiopen
from io import BytesIO
from collections import namedtuple
import ujson
import boto3
import os
import asyncio
import gc


class ItemsModelsCollectionDataImporter(ItemsModelsCollection):

    async def post_import_data_file_job(self, req, session):
        content_type = req.headers.get('content-type')
        if content_type != 'application/zip':
            raise SwaggerItModelError("Invalid content type '{}'".format(content_type))

        stream = BytesIO()
        async for line in req.body:
            stream.write(line)
        stream.seek(0)

        return self._create_job(
            self._run_import_data_file_job,
            req, session, '_importer', stream=stream)

    def _create_job(self, func, req, session, jobs_id_prefix, stream=None):
        session = self._copy_session(session)
        store_id = req.query['store_id']
        items_model = self._get_model(req.query)
        jobs_id = items_model.__key__ + jobs_id_prefix
        return super()._create_job(
            func, jobs_id,
            req, session,
            items_model, store_id,
            stream=stream
        )

    def _run_import_data_file_job(self, req, session, items_model, store_id, stream):
        upload_file = req.query.get('upload_file', True)
        if upload_file:
            self._put_file_on_s3(stream, items_model, session, store_id)
            stream.seek(0)

        result = asyncio.run_coroutine_threadsafe(
            self._update_items_from_zipped_file(stream, items_model, session),
            session.loop
        ).result()

        gc.collect()
        return result

    def _put_file_on_s3(self, stream, items_model, session, store_id):
        self._logger.info("Started put file on S3 for '{}'".format(items_model.__key__))

        store = asyncio.run_coroutine_threadsafe(
            self.get_model('stores').get(session, [{'id': store_id}]),
            session.loop
        ).result()[0]

        s3_bucket = store['configuration']['aws']['s3']['bucket']
        access_key_id = store['configuration']['aws'].get('access_key_id')
        secret_access_key = store['configuration']['aws'].get('secret_access_key')
        s3_key = '{}.zip'.format(items_model.__key__)

        boto3.resource(
            's3',
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key
        ).Bucket(s3_bucket).put_object(Body=stream, Key=s3_key)

        self._logger.info("Finished put file on S3 for '{}'".format(items_model.__key__))

    async def _update_items_from_zipped_file(self, stream, items_model, session):
        feed = await self._build_async_file(zip_open, stream)
        result = await self._update_items_from_file(feed, items_model, session)

        await feed.close()
        os.remove(feed.name)
        return result

    async def _build_async_file(self, file_func, stream=None, mode=None):
        tempfile = NamedTemporaryFile(delete=False)
        filename = tempfile.name
        kwargs = {'mode': mode} if mode else {}
        if stream is not None:
            tempfile.write(stream.read())

        tempfile.close()
        af = await file_func(filename, **kwargs)
        af.name = filename
        return af

    async def _update_items_from_file(self, feed, items_model, session):
        self._logger.info("Started update items from file for '{}'".format(items_model.__key__))


        old_keys = set(await session.redis_bind.hkeys(items_model.__key__))
        import_processor = self._get_import_processor()
        new_keys, status, new_feed = \
            await self._pre_processing_import(feed, items_model, import_processor)
        lines = []

        async for line in new_feed:
            lines.append(eval(line))

            if len(lines) == 1000:
                self._post_processing_import(lines, status, import_processor)
                await items_model.insert(session, lines)
                lines = []

        if lines:
            self._post_processing_import(lines, status, import_processor)
            await items_model.insert(session, lines)

        del lines
        await new_feed.close()
        old_keys.difference_update(new_keys)

        if old_keys:
            await session.redis_bind.hdel(items_model.__key__, *old_keys)

        await self._set_stock_filter(session, items_model)

        self._logger.info("Finished update items from file for '{}'".format(items_model.__key__))

        return status

    def _get_import_processor(self):
        import_processor = self.__item_type__['import_processor']
        if import_processor is not None:
            return ModuleObjectLoader.load(import_processor)

    async def _pre_processing_import(self, feed, items_model, import_processor):
        warning_message = "Invalid line for model '{}': ".format(items_model.__key__) + '{}'
        validator = Draft4Validator(items_model.__item_type__['schema'])
        new_keys = set()
        success_lines = 0
        errors_lines = 0
        empty_lines = 0
        new_feed = await self._build_async_file(aiopen, mode='w')

        async for line in feed:
            try:
                line = self._try_to_process_line(line, validator)
                if line is None:
                    empty_lines += 1
                    continue
                else:
                    success_lines += 1
                    new_keys.add(items_model.get_instance_key(line))
                    await self._write_new_feed(new_feed, line, import_processor)

            except Exception as error:
                errors_lines += 1
                self._logger.warning(warning_message.format(line))
                self._logger.warning(error)
                continue

        await new_feed.close()
        status = {
            'success_lines': success_lines,
            'errors_lines': errors_lines,
            'empty_lines': empty_lines
        }
        return new_keys, status, await aiopen(new_feed.name)

    def _try_to_process_line(self, line, validator):
        line = line.strip().decode()
        if not line:
            return None

        line = ujson.loads(line)
        validator.validate(line)

        return line

    async def _write_new_feed(self, new_feed, line, import_processor):
        if import_processor is not None and hasattr(import_processor, 'pre_execute'):
            import_processor.pre_execute(line)

        await new_feed.write(repr(line) + '\n')

    def _post_processing_import(self, lines, status, import_processor):
        if import_processor is not None:
            import_processor.execute(lines, status)

    async def get_import_data_file_job(self, req, session):
        jobs_id = self._get_model(req.query).__key__ + '_importer'
        return await self._get_job(jobs_id, req, session)
