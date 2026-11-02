"""Native Windows child ownership. No PID-based termination API is exposed."""
import ctypes as c
from ctypes import wintypes as w
import os
import subprocess


class UnsupportedSupervision(RuntimeError):
    pass


class STARTUPINFO(c.Structure):
    _fields_ = [('cb',w.DWORD),('lpReserved',w.LPWSTR),('lpDesktop',w.LPWSTR),
        ('lpTitle',w.LPWSTR),('dwX',w.DWORD),('dwY',w.DWORD),('dwXSize',w.DWORD),
        ('dwYSize',w.DWORD),('dwXCountChars',w.DWORD),('dwYCountChars',w.DWORD),
        ('dwFillAttribute',w.DWORD),('dwFlags',w.DWORD),('wShowWindow',w.WORD),
        ('cbReserved2',w.WORD),('lpReserved2',c.POINTER(c.c_byte)),
        ('hStdInput',w.HANDLE),('hStdOutput',w.HANDLE),('hStdError',w.HANDLE)]


class PROCESSINFO(c.Structure):
    _fields_ = [('process',w.HANDLE),('thread',w.HANDLE),('pid',w.DWORD),('tid',w.DWORD)]


class BASICLIMIT(c.Structure):
    _fields_ = [('process_time',c.c_longlong),('job_time',c.c_longlong),('flags',w.DWORD),
        ('min_working',c.c_size_t),('max_working',c.c_size_t),('active_processes',w.DWORD),
        ('affinity',c.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]


class IOCOUNTERS(c.Structure):
    _fields_ = [(n,c.c_ulonglong) for n in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]


class EXTENDEDLIMIT(c.Structure):
    _fields_ = [('basic',BASICLIMIT),('io',IOCOUNTERS),('process_memory',c.c_size_t),
        ('job_memory',c.c_size_t),('peak_process',c.c_size_t),('peak_job',c.c_size_t)]


class Windows:
    def __init__(self):
        if os.name != 'nt':
            raise UnsupportedSupervision('WINDOWS_JOB_UNSUPPORTED_PLATFORM')
        self.k = c.WinDLL('kernel32',use_last_error=True)
        signatures = {
            'CreateJobObjectW': ([c.c_void_p,w.LPCWSTR],w.HANDLE),
            'OpenJobObjectW': ([w.DWORD,w.BOOL,w.LPCWSTR],w.HANDLE),
            'SetInformationJobObject': ([w.HANDLE,c.c_int,c.c_void_p,w.DWORD],w.BOOL),
            'AssignProcessToJobObject': ([w.HANDLE,w.HANDLE],w.BOOL),
            'IsProcessInJob': ([w.HANDLE,w.HANDLE,c.POINTER(w.BOOL)],w.BOOL),
            'CreateProcessW': ([w.LPCWSTR,w.LPWSTR,c.c_void_p,c.c_void_p,w.BOOL,w.DWORD,c.c_void_p,w.LPCWSTR,c.POINTER(STARTUPINFO),c.POINTER(PROCESSINFO)],w.BOOL),
            'ResumeThread': ([w.HANDLE],w.DWORD),
            'TerminateProcess': ([w.HANDLE,w.UINT],w.BOOL),
            'TerminateJobObject': ([w.HANDLE,w.UINT],w.BOOL),
            'WaitForSingleObject': ([w.HANDLE,w.DWORD],w.DWORD),
            'GetExitCodeProcess': ([w.HANDLE,c.POINTER(w.DWORD)],w.BOOL),
            'GetProcessTimes': ([w.HANDLE,c.POINTER(w.FILETIME),c.POINTER(w.FILETIME),c.POINTER(w.FILETIME),c.POINTER(w.FILETIME)],w.BOOL),
            'OpenProcess': ([w.DWORD,w.BOOL,w.DWORD],w.HANDLE),
            'GetCurrentProcess': ([],w.HANDLE),
            'CloseHandle': ([w.HANDLE],w.BOOL),
        }
        for name,(args,result) in signatures.items():
            f=getattr(self.k,name);f.argtypes=args;f.restype=result

    @staticmethod
    def check(ok):
        if not ok:
            raise c.WinError(c.get_last_error())

    def creation(self, handle):
        values=[w.FILETIME() for _ in range(4)]
        self.check(self.k.GetProcessTimes(handle,*(c.byref(x) for x in values)))
        return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime

    def parent_identity(self):
        return {'pid':os.getpid(),'creation_filetime':self.creation(self.k.GetCurrentProcess())}

    def verify_child(self, child, parent):
        """Child-side independent OS verification; no receipt label is proof."""
        if self.parent_identity() != {k:child.get(k) for k in ('pid','creation_filetime')}:
            raise UnsupportedSupervision('CHILD_CREATION_IDENTITY_MISMATCH')
        if os.getppid() != parent.get('pid'):
            raise UnsupportedSupervision('CHILD_PARENT_MISMATCH')
        handle = self.k.OpenProcess(0x1000|0x100000,False,parent['pid'])
        self.check(handle)
        try:
            if self.creation(handle) != parent.get('creation_filetime') or self.k.WaitForSingleObject(handle,0) != 258:
                raise UnsupportedSupervision('PARENT_CREATION_IDENTITY_MISMATCH')
        finally:
            self.k.CloseHandle(handle)
        job = self.k.OpenJobObjectW(0x4,False,child['job_name']) # JOB_OBJECT_QUERY
        self.check(job)
        try:
            belongs = w.BOOL()
            self.check(self.k.IsProcessInJob(self.k.GetCurrentProcess(),job,c.byref(belongs)))
            if not belongs.value:
                raise UnsupportedSupervision('CHILD_JOB_MEMBERSHIP_MISMATCH')
        finally:
            self.k.CloseHandle(job)
        return {'status':'PASS','child_identity':'OS_VERIFIED','parent_identity':'OS_VERIFIED','job_membership':'OS_VERIFIED'}

    def spawn(self, command, cwd, env, invocation_id):
        job = self.k.CreateJobObjectW(None, 'Local\\SwarmEval-'+invocation_id)
        self.check(job)
        # A collision is not ownership, even for a cryptographically fresh name.
        if c.get_last_error() == 183:
            self.k.CloseHandle(job)
            raise UnsupportedSupervision('JOB_NAME_COLLISION')
        pi = PROCESSINFO()
        try:
            limits=EXTENDEDLIMIT();limits.basic.flags=0x2000 # KILL_ON_JOB_CLOSE
            self.check(self.k.SetInformationJobObject(job,9,c.byref(limits),c.sizeof(limits)))
            si=STARTUPINFO();si.cb=c.sizeof(si)
            command_line=c.create_unicode_buffer(subprocess.list2cmdline(command))
            environment=c.create_unicode_buffer('\0'.join(k+'='+v for k,v in sorted(env.items(),key=lambda x:x[0].upper()))+'\0\0')
            self.check(self.k.CreateProcessW(command[0],command_line,None,None,False,
                0x4|0x400|0x200|0x08000000,environment,str(cwd),c.byref(si),c.byref(pi)))
            self.check(self.k.AssignProcessToJobObject(job,pi.process))
            belongs=w.BOOL()
            self.check(self.k.IsProcessInJob(pi.process,job,c.byref(belongs)))
            if not belongs.value:
                raise UnsupportedSupervision('JOB_ASSOCIATION_NOT_OBSERVED')
            identity={'pid':pi.pid,'creation_filetime':self.creation(pi.process),
                'job_name':'Local\\SwarmEval-'+invocation_id,'job_association':'OS_VERIFIED',
                'process_group':pi.pid,'ownership':'SUSPENDED_ASSIGNED_BEFORE_RESUME'}
            # Return still suspended: durable ownership must publish before execution.
            return OwnedChild(self,job,pi,identity)
        except BaseException:
            if pi.process:
                self.k.TerminateProcess(pi.process,125)
                self.k.WaitForSingleObject(pi.process,5000)
                self.k.CloseHandle(pi.process)
            if pi.thread:self.k.CloseHandle(pi.thread)
            self.k.CloseHandle(job)
            raise

    def probe(self, identity):
        handle=self.k.OpenProcess(0x1000|0x100000,False,identity['pid'])
        if not handle:
            error=c.get_last_error()
            return {'state':'DEAD' if error==87 else 'UNKNOWN','os_error':error,'exit_code':None}
        try:
            if self.creation(handle)!=identity['creation_filetime']:
                return {'state':'PID_REUSED','exit_code':None}
            wait=self.k.WaitForSingleObject(handle,0)
            if wait==0:
                code=w.DWORD();self.check(self.k.GetExitCodeProcess(handle,c.byref(code)))
                return {'state':'DEAD','exit_code':code.value}
            return {'state':'RUNNING' if wait==258 else 'UNKNOWN','exit_code':None}
        finally:self.k.CloseHandle(handle)


class OwnedChild:
    def __init__(self, backend, job, pi, identity):
        self.backend,self.job,self.pi,self.identity=backend,job,pi,identity
        self.resume_state='SPAWNED_SUSPENDED'

    def resume(self):
        self.resume_state='RESUME_OUTCOME_UNKNOWN'
        if self.backend.k.ResumeThread(self.pi.thread)==0xffffffff:
            raise c.WinError(c.get_last_error())
        # Preserve the successful OS operation even if later bookkeeping fails.
        self.resume_state='RESUME_CONFIRMED'
        self.backend.k.CloseHandle(self.pi.thread);self.pi.thread=None

    def poll(self):
        result=self.backend.k.WaitForSingleObject(self.pi.process,0)
        if result==258:return None
        if result!=0:raise c.WinError(c.get_last_error())
        code=w.DWORD();self.backend.check(self.backend.k.GetExitCodeProcess(self.pi.process,c.byref(code)))
        return code.value

    def terminate(self):
        self.backend.check(self.backend.k.TerminateJobObject(self.job,124))

    def close(self):
        for handle in (self.pi.thread,self.pi.process,self.job):
            if handle:self.backend.k.CloseHandle(handle)
        self.pi.thread=self.pi.process=self.job=None
