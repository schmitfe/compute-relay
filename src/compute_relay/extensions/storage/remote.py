"""Fixed remote statvfs helper; no agent source, scans, content reads or writes."""

REMOTE_SOURCE = r'''
import datetime,json,os,re,sys,time,uuid
SAFE=2**53-1
def pairs(values):
    result={}
    for key,value in values:
        if key in result: raise ValueError('duplicate key')
        result[key]=value
    return result
def integer(value):
    if type(value) is not int or not 0<=value<=SAFE: raise ValueError('counter range')
    return value
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def unknown(code):
    return {'schema_version':'1.0','source':'statvfs','status':'unavailable','observed_at':now(),
        'filesystem_id':None,'identity_verified':False,'total_bytes':None,'available_bytes':None,
        'total_inodes':None,'available_inodes':None,'quota':{'status':'unknown'},'error_code':code}
def probe(request):
    fields={'operation','context','root','expected_filesystem_id'}
    if set(request)!=fields or request['operation']!='probe_storage': raise ValueError('fixed operation required')
    context=request['context']
    if not isinstance(context,dict) or set(context)!={'pool_id','target_id','connection_id','root_alias','profile_revision_sha256','connection_revision_sha256'}: raise ValueError('context')
    for key in ('pool_id','target_id','connection_id'):
        if str(uuid.UUID(context[key]))!=context[key]: raise ValueError('identity')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}',context['root_alias']): raise ValueError('alias')
    for key in ('profile_revision_sha256','connection_revision_sha256'):
        if not re.fullmatch(r'[0-9a-f]{64}',context[key]): raise ValueError('binding')
    root=request['root']
    if not isinstance(root,str) or len(root)>4096 or not re.fullmatch(r'/[A-Za-z0-9_./-]+',root) or root=='/' or root.startswith('//') or os.path.normpath(root)!=root: raise ValueError('root')
    parts=root[1:].split('/')
    if len(parts)>64 or any(part in ('','.','..') for part in parts): raise ValueError('depth')
    expected=request['expected_filesystem_id']
    if expected is not None and (not isinstance(expected,str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,128}',expected)): raise ValueError('filesystem identity')
    deadline=time.monotonic()+2
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in parts:
            if time.monotonic()>=deadline: raise TimeoutError('deadline')
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        info,stats=os.fstat(fd),os.fstatvfs(fd)
        if time.monotonic()>=deadline: raise TimeoutError('deadline')
        filesystem_id=str(info.st_dev)+':'+str(stats.f_fsid)
        total_bytes=integer(integer(stats.f_blocks)*integer(stats.f_frsize))
        available_bytes=integer(integer(stats.f_bavail)*integer(stats.f_frsize))
        total_inodes,available_inodes=stats.f_files,stats.f_favail
        if type(total_inodes) is not int or not 0<total_inodes<=SAFE or type(available_inodes) is not int or not 0<=available_inodes<=SAFE:
            total_inodes=available_inodes=None
        else:
            total_inodes,available_inodes=integer(total_inodes),integer(available_inodes)
        if available_bytes>total_bytes or total_inodes is not None and available_inodes>total_inodes: raise ValueError('invalid counters')
        verified=bool(expected and expected==filesystem_id)
        data={'schema_version':'1.0','source':'statvfs','status':'available','observed_at':now(),
            'filesystem_id':filesystem_id,'identity_verified':verified,'total_bytes':total_bytes,
            'available_bytes':available_bytes,'total_inodes':total_inodes,'available_inodes':available_inodes,
            'quota':{'status':'unknown'}}
        if expected and not verified: data.update(status='unavailable',error_code='FILESYSTEM_IDENTITY_MISMATCH')
        return {'context':context,'observation':data}
    finally: os.close(fd)
try:
    incoming=sys.stdin.buffer.read(16385)
    if len(incoming)>16384: raise ValueError('request limit')
    request=json.loads(incoming,object_pairs_hook=pairs,parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite')))
    print(json.dumps(probe(request),separators=(',',':'),allow_nan=False))
except Exception:
    print(json.dumps({'observation':unknown('STORAGE_METADATA_UNAVAILABLE')},separators=(',',':')))
'''
