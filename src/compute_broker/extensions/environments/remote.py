"""Fixed, metadata-only environment finalizer. No import of built environment code."""

REMOTE_SOURCE = r'''
import hashlib,json,os,stat,sys,uuid
NOFOLLOW=os.O_NOFOLLOW
DIRECTORY=os.O_DIRECTORY
def pairs(items):
    result={}
    for key,value in items:
        if key in result: raise ValueError('duplicate JSON key')
        result[key]=value
    return result
def load(data): return json.loads(data,object_pairs_hook=pairs)
def uid(value):
    if not isinstance(value,str) or str(uuid.UUID(value))!=value: raise ValueError('invalid UUID')
    return value
def parts(path):
    if not isinstance(path,str) or not path or len(path)>4096 or any(ord(c)<32 or ord(c)==127 for c in path): raise ValueError('invalid path')
    value=path.split('/')
    if any(item in {'','.','..'} for item in value): raise ValueError('invalid component')
    return value
def directory(path,parent=None):
    if parent is None:
        if not path.startswith('/') or path=='/': raise ValueError('absolute root required')
        fd=os.open('/',os.O_RDONLY|DIRECTORY|NOFOLLOW);components=parts(path[1:])
    else:
        fd=os.dup(parent);components=parts(path)
    try:
        for name in components:
            other=os.open(name,os.O_RDONLY|DIRECTORY|NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=other
        return fd
    except BaseException:
        os.close(fd);raise
def regular(parent,path):
    components=parts(path);fd=os.dup(parent)
    try:
        for name in components[:-1]:
            other=os.open(name,os.O_RDONLY|DIRECTORY|NOFOLLOW,dir_fd=fd);os.close(fd);fd=other
        result=os.open(components[-1],os.O_RDONLY|NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        info=os.fstat(result)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
            os.close(result);raise ValueError('regular single-link file required')
        return result
    finally: os.close(fd)
def read(parent,path,limit):
    fd=regular(parent,path)
    try:
        before=os.fstat(fd)
        if before.st_size>limit: raise ValueError('metadata exceeds limit')
        data=b''
        while len(data)<=limit:
            piece=os.read(fd,min(65536,limit+1-len(data)))
            if not piece: break
            data+=piece
        after=os.fstat(fd)
        if len(data)>limit or (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns): raise ValueError('metadata changed')
        return data
    finally: os.close(fd)
def finalize(request):
    if request.get('operation')!='finalize_environment': raise ValueError('unsupported operation')
    identity=request['native_identity'];attempt=uid(identity['attempt_id']);build=uid(request['build_id']);spec=request['spec_sha256']
    if not isinstance(spec,str) or len(spec)!=64 or any(c not in '0123456789abcdef' for c in spec): raise ValueError('invalid digest')
    root_path=request['config']['remote_root'];root=directory(root_path)
    try:
        journal=load(read(root,'_broker_journal/'+attempt+'.json',1048576))
        if journal.get('outcome')!='submitted' or journal.get('native_identity')!=identity: raise ValueError('unowned attempt')
        manifest=journal['manifest'];job=uid(manifest['job_revision_id']);project=uid(manifest['request']['project_id'])
        if manifest.get('extension_name')!='environments': raise ValueError('wrong extension binding')
        relative='/'.join([project,job,attempt])
        if journal.get('attempt_path')!=relative or identity.get('work_dir')!=root_path.rstrip('/')+'/'+relative: raise ValueError('directory identity mismatch')
        if manifest['request']['command']['script']!='_environment/build.py' or '_environment/receipt.json' not in manifest['request']['outputs']: raise ValueError('not a declared build')
        attempt_dir=directory(relative,parent=root)
        try:
            recipe_bytes=read(attempt_dir,'_environment/recipe.json',131072)
            descriptors={item['path']:item for item in manifest['files']}
            if hashlib.sha256(recipe_bytes).hexdigest()!=descriptors['_environment/recipe.json']['sha256']: raise ValueError('recipe changed')
            recipe=load(recipe_bytes)
            if recipe.get('build_id')!=build or recipe.get('spec_sha256')!=spec: raise ValueError('recipe identity mismatch')
            receipt=load(read(attempt_dir,'_environment/receipt.json',32768))
            if receipt.get('build_id')!=build or receipt.get('spec_sha256')!=spec or receipt.get('attempt_id')!=attempt or receipt.get('validated') is not True: raise ValueError('receipt identity mismatch')
            if receipt.get('manager')!='venv' or receipt.get('prefix_relative')!='environment' or receipt.get('interpreter_relative')!='environment/bin/python': raise ValueError('unsupported prefix')
            environment=directory('environment',parent=attempt_dir)
            try:
                info=os.fstat(environment)
                if info.st_uid!=os.getuid() or info.st_mode&0o022: raise ValueError('unsafe prefix ownership/permissions')
                bin_dir=directory('bin',parent=environment)
                try:
                    info=os.fstat(bin_dir)
                    if info.st_uid!=os.getuid() or info.st_mode&0o022: raise ValueError('unsafe executable directory')
                    for parent,path in ((environment,'pyvenv.cfg'),(bin_dir,'python')):
                        fd=regular(parent,path)
                        try:
                            info=os.fstat(fd)
                            if info.st_uid!=os.getuid() or info.st_mode&0o022 or (path=='python' and not info.st_mode&0o111): raise ValueError('unsafe environment file')
                        finally: os.close(fd)
                finally: os.close(bin_dir)
            finally: os.close(environment)
            return {'verified':True,'build_id':build,'spec_sha256':spec,'attempt_id':attempt,
                'prefix':identity['work_dir']+'/environment','receipt':receipt}
        finally: os.close(attempt_dir)
    finally: os.close(root)
try:
    incoming=sys.stdin.buffer.read(65537)
    if len(incoming)>65536: raise ValueError('request exceeds limit')
    print(json.dumps(finalize(load(incoming)),separators=(',',':')))
except Exception:
    print(json.dumps({'verified':False,'error_code':'ENVIRONMENT_METADATA_UNVERIFIED'}))
'''
