"""Separate generated tools use the real SDK against a fake Unix HTTP daemon.
This checks process-wide slot races and API wire shape, not Docker isolation.
"""
import json
import os
from pathlib import Path
import socketserver
from http.server import BaseHTTPRequestHandler
import subprocess
import sys
import threading
import time
import pytest
from urllib.parse import urlsplit,parse_qs
BASE=Path(__file__).resolve().parents[1]

def test_process_wide_capacity(tmp_path):
    items={};lock=threading.Lock();gate=threading.Event();peak=[0];serial=[0]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def reply(self,code,data):
            payload=json.dumps(data).encode()
            self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        def find(self,path):
            identifier=path.split('/containers/')[1].split('/')[0]
            return next((x for x in items.values() if x['Id']==identifier),None)
        def do_GET(self):
            path=urlsplit(self.path).path
            if path=='/version':return self.reply(200,{'ApiVersion':'1.45','Version':'fake'})
            if path.endswith('/info'):return self.reply(200,{'OSType':'linux','MemoryLimit':True,'SwapLimit':True,'CpuCfsQuota':True})
            if '/images/' in path:return self.reply(200,{'Id':'sha256:'+'a'*64,'Config':{'Cmd':['echo'],'Entrypoint':None,'Volumes':{}}})
            with lock:
                if path.endswith('/containers/json'):return self.reply(200,[{'Id':x['Id']} for x in items.values()])
                obj=self.find(path)
                if obj is None:return self.reply(404,{'message':'not found'})
                if path.endswith('/logs'):
                    self.send_response(200);self.send_header('Content-Length','0');self.end_headers();return
                return self.reply(200,obj)
        def do_POST(self):
            split=urlsplit(self.path);path=split.path
            size=int(self.headers.get('Content-Length','0'));body=json.loads(self.rfile.read(size)) if size else {}
            if path.endswith('/containers/create'):
                name=parse_qs(split.query)['name'][0]
                with lock:
                    if name in items:return self.reply(409,{'message':'name in use'})
                    serial[0]+=1
                    obj={'Id':str(serial[0]).zfill(64),'Name':'/'+name,'Config':{'Labels':body['Labels'],'Tty':False},'Mounts':[],'State':{'Running':False,'OOMKilled':False,'ExitCode':0}}
                    items[name]=obj;peak[0]=max(peak[0],len(items))
                    assert body['HostConfig']['NanoCpus']==1000000000
                    assert body['HostConfig']['NetworkMode']=='none'
                    return self.reply(201,{'Id':obj['Id'],'Warnings':[]})
            with lock:obj=self.find(path)
            if obj is None:return self.reply(404,{'message':'not found'})
            if path.endswith('/start'):
                obj['State']['Running']=True;return self.reply(204,{})
            if path.endswith('/wait'):
                gate.wait(10);obj['State']['Running']=False;return self.reply(200,{'StatusCode':0})
            if path.endswith('/kill'):
                obj['State']['Running']=False;return self.reply(204,{})
            return self.reply(404,{})
        def do_DELETE(self):
            with lock:
                obj=self.find(urlsplit(self.path).path)
                if obj is None:return self.reply(404,{'message':'not found'})
                items.pop(obj['Name'][1:]);return self.reply(204,{})
    class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
        daemon_threads=True
    # Keep Unix socket pathname below Linux's 108-byte limit.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='tw-dkr-') as socketdir:
        sock=str(Path(socketdir)/'engine.sock')
        try: server=Server(sock,Handler)
        except PermissionError: pytest.skip('This execution environment disallows Unix sockets')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        root=tmp_path/'root';root.mkdir()
        env={**os.environ,'DOCKER_HOST':'unix://'+sock,'TWYLT_WORKSPACE_ROOT':str(root),'TWYLT_INCIDENT_LOG':str(tmp_path/'audit.jsonl'),'TWYLT_DOCKER_MAX_CONTAINERS':'2','TWYLT_DOCKER_ALLOW_PULL':'false'}
        processes=[]
        try:
            for _ in range(8):
                processes.append(subprocess.Popen([sys.executable,str(BASE/'tools/docker_run/run.py'),'{"image":"alpine:3.20","timeout":20}'],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
            end=time.monotonic()+10
            while time.monotonic()<end:
                with lock:n=len(items)
                if n==2:break
                time.sleep(.02)
            assert n==2
            # Let the other independent processes attempt their atomic reservations.
            end=time.monotonic()+5
            while sum(p.poll() is not None for p in processes)<6 and time.monotonic()<end:time.sleep(.02)
            gate.set()
            results=[]
            for p in processes:
                out,err=p.communicate(timeout=15)
                assert p.returncode==0,err
                results.append(json.loads(out))
            assert peak[0]==2 and not items
            assert sum(x['status']=='busy' for x in results)==6
            assert sum(x['ok'] for x in results)==2,results
        finally:
            gate.set()
            for p in processes:
                if p.poll() is None:p.kill();p.communicate()
            server.shutdown();server.server_close()
