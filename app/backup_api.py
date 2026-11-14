import tempfile
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import backups, config
from .backup_storage import StorageError, remote, valid_name
from .domain import owner
from .db import authenticated_session

router = APIRouter(prefix='/api/backups')


def authorize(request, scope=None):
    from .main import current, limited
    owner(current(request))
    if scope:
        limited(request, scope, 5 if scope == 'backup-restore' else 10)
    return authenticated_session.get()


@router.get('/config')
def backup_config(request: Request):
    authorize(request)
    return backups.public_config()


@router.put('/config')
def update_backup_config(body: dict, request: Request):
    authorize(request)
    return backups.save_config(body)


@router.get('')
def backup_list(request: Request, target: str = 'local'):
    authorize(request)
    return backups.list_backups(target)


@router.post('')
def backup_create(body: dict, request: Request):
    authorize(request, 'backup-create')
    return backups.create_backup(body.get('targets', [body.get('target', 'local')]))


@router.post('/test')
def backup_test(body: dict, request: Request):
    authorize(request, 'backup-test')
    return backups.test_storage(body.get('target', 'local'))


@router.get('/download/{name}')
def backup_download(name: str, request: Request, target: str = 'local'):
    authorize(request)
    if target == 'local':
        return FileResponse(backups.local_path(name), media_type='application/zip', filename=name)
    conf = backups.read_control()['config']
    backups.require_target(target, conf)
    if not valid_name(name):
        raise backups.BackupError('备份文件名不正确')
    directory = tempfile.TemporaryDirectory(prefix='.backup-download-', dir=config.DATA)
    path = Path(directory.name) / name
    try:
        remote(target, conf).download(name, path, backups.max_bytes())
        return FileResponse(path, media_type='application/zip', filename=name,
                            background=BackgroundTask(directory.cleanup))
    except StorageError as exc:
        directory.cleanup()
        raise backups.BackupError(str(exc), 502) from None
    except Exception:
        directory.cleanup()
        raise


@router.post('/restore')
def backup_restore(body: dict, request: Request, response: Response):
    authorize(request, 'backup-restore')
    if body.get('confirm') is not True:
        raise backups.BackupError('请先确认覆盖当前账本')
    result = backups.restore_backup(body.get('target', 'local'), body.get('name'))
    response.delete_cookie('ledger_session', path='/')
    return result


@router.post('/restore/upload')
async def backup_restore_upload(request: Request, response: Response):
    token_hash = await run_in_threadpool(authorize, request, 'backup-restore')
    authenticated_session.set(token_hash)
    if request.headers.get('x-backup-confirm') != 'restore':
        raise backups.BackupError('请先确认覆盖当前账本')
    with tempfile.TemporaryDirectory(prefix='.backup-upload-', dir=config.DATA) as directory:
        path = Path(directory) / 'uploaded.zip'
        total = 0
        with path.open('wb') as stream:
            async for chunk in request.stream():
                total += len(chunk)
                if total > backups.max_bytes():
                    raise backups.BackupError('备份文件超过服务器允许的大小', 413)
                await run_in_threadpool(stream.write, chunk)
        if not total:
            raise backups.BackupError('请选择备份 ZIP 文件')
        result = await run_in_threadpool(backups.restore_backup, uploaded=path)
    response.delete_cookie('ledger_session', path='/')
    return result
