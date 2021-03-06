import json
import hashlib
import random
import threading
import uuid
import time
import base64

import itc

import tcp
import timer
import event
import mrq
import maekawa

class Broadcaster(object):
    '''
    Implements an overlay network designed to minimize the number
    of hops in a network broadcast.

    Except "designed" doesn't really convey the amount of flailing
    going on, here.
    '''
    def __init__(self, bootstrap=(), port=6966, heartbeat=30, joincb=None):
        self.c = 0
        self.peers = {}
        self.clist = {}
        self.uuid = uuid.uuid4().int
        self.value = (0, 0)
        self.lace_max = (0, 0)
        self.tcp = tcp.TCP(port)
        self.seen = mrq.MRQ(2500)
        self.tcp.handlers += self.handle_tcp_msg
        self.lock = threading.RLock()
        self.plock = threading.Lock()
        self.timer = timer.Timer(heartbeat)
        self.handlers = event.Event()
        self.boot = bootstrap
        self.joincb = joincb
        self.clock = None
        self.testid = 0
        self.mk = maekawa.MaekawaNode(self)

    def base(self):
        self.value = self.lace_max = (1, 1)
        self.clock = itc.Stamp()

    def addpeer(self, pid, contact):
        pl = self.peers.get(pid, {'contact': None, 'value': (0, 0)})
        pl['contact'] = contact
        self.peers[pid] = pl

    def dumpstamp(self, stamp):
        return base64.b64encode(stamp.dump())

    def loadstamp(self, stampstr):
        return itc.Stamp.load(base64.b64decode(stampstr))

    def start(self):
        self.tcp.start()
        if self.boot:
            self.bootstrap(self.boot)
        self.timer.start()

    def stop(self):
        with self.lock:
            for p in self.peers.values():
                self.bye(p)
            self.timer.disable()
            self.tcp.shutdown()

    def mkmsg(self, msgtype='noop'):
        msg = {}
        msg['type'] = msgtype
        msg['id'] = (self.uuid, self.value)
        msg['stamp'] = uuid.uuid4().int
        msg['srvport'] = self.tcp.port
        if self.clock:
            msg['clock'] = self.dumpstamp(self.clock.peek())
        return msg

    def send(self, data):
        msg = self.mkmsg()
        msg['data'] = data
        msg['type'] = 'data'
        self.broadcast(msg)

    def send_one(self, data):
        msg = self.mkmsg()
        msg['data'] = data
        msg['type'] = 'oncedata'
        dst = None
        with self.lock:
            if len(self.peers) > 0:
                dst = random.choice(self.peers.keys())
        if dst:
            self.sendmsg(msg, dst)

    def bootstrap(self, plist):
        msg = self.mkmsg('hello')
        for p in plist:
            self.sendmsg_raw(msg, p)

    def handle_tcp_msg(self, msg, conn):
        self.handle_msg(msg, conn)

    def handle_msg(self, msg, conn):
        '''
        msg is a json-encoded message
        '''
        if conn:
            addr = conn.getpeername()
        else:
            addr = (0, 0)
        if not addr in self.clist and not addr == (0, 0):
            self.clist[addr] = conn
        with self.lock:
            msg = json.loads(msg)
            if msg['stamp'] in self.seen:
                return
            self.seen += msg['stamp']
            src = msg.get('src', None) or addr
            src = src[0], src[1]
            if not msg.get('src', None):
                msg['src'] = src
            addr = tuple(msg['src'])
            def reply(data):
                rmsg = self.mkmsg()
                rmsg['type'] = 'oncedata'
                rmsg['data'] = data
                self.sendmsg(rmsg, msg['id'])
            try:
                handler = getattr(self, "handle_msg_%s"%msg['type'])
            except AttributeError:
                print "no such handler"
                return
            handler(msg, addr, reply)

    def get_next_addr(self, addr):
        '''
        So the way we fill out the lace is:

            1 2 5
            3 4 7
            6 8 9

        which pattern is (1, 1), [we are 1-indexed, here] (2, 1),
        (1, 2), (2, 2), (3, 1), (1, 3), (3, 2), (2, 3), (3, 3), etc.

        The rule for generating this pattern is, for any tuple (x, y):

        (x, y) -> (x+1, 1) when x = y
        (x, y) -> (y, x) when x > y
        (x, y) -> (y, x+1) when x < y
        '''
        x = addr[0]
        y = addr[1]
        if x == y:
            return (x + 1, 1)
        elif x > y:
            return (y, x)
        elif x < y:
            return (y, x+1)
        else:
            # son, you've got issues
            return (0, 0)

    def ispeer(self, value):
        if value[0] == self.value[0] or value[1] == self.value[1]:
            return True
        return False

    def acquire(self, acqcb=None):
        with self.lock:
            self.mk.acquire(acqcb)

    def release(self):
        with self.lock:
            self.mk.release()

    class mutob(object):
        def __init__(self, bc):
            self.bc = bc
            self.ev = threading.Event()

        def __enter__(self):
            self.bc.acquire(self.ev.set)
            a = self.ev.wait(2)
            if not a:
                print "fail on", self.bc.uuid % 997
                print len(self.bc.mk.fails), len(self.bc.mk.grants), len(self.bc.mk.grantset), self.bc.mk.mutexed
                raise RuntimeError("deadlock")

        def __exit__(self, type, value, traceback):
            self.bc.release()

    def mutex(self):
        return self.mutob(self)

    def bumptid(self):
        self.testid += 1
        m = self.mkmsg('bumptid')
        m['testid'] = self.testid
        self.broadcast(m)

    def handle_msg_bumptid(self, msg, addr, reply):
        self.broadcast(msg)
        self.testid = msg['testid']

    def handle_msg_maekawa(self, msg, addr, reply):
        self.mk.handle_msg(msg)

    def handle_msg_newlm(self, msg, addr, reply):
        '''
        Handle 'newlm' message, bumping the lace_max
        '''
        self.broadcast(msg)
        oclock = self.loadstamp(msg['clock'])
        if self.clock <= oclock:
            self.lace_max = tuple(msg['newlm'])
            self.clock = self.clock + oclock

    def handle_msg_recon(self, msg, addr, reply):
        '''
	    The 'reconnect' message; just start all over and grab new
    	state.
        '''
        self.value = self.lace_max = (0, 0)
        self.stamp = None
        self.bootstrap([addr])

    def handle_msg_needpeer(self, msg, addr, reply):
        '''
	    Handle the 'needpeer' message.  addr is seeking peers.
        '''
        self.broadcast(msg)
        if not self.ispeer(msg['id'][1]):
            # not a concern of ours
            return
        if tuple(msg['id'][1]) == self.value:
            # someone got handed our id
            nmsg = self.mkmsg()
            nmsg['type'] = 'recon'
            self.sendmsg(nmsg, addr)
            return
        pid = msg['id'][0]
        if not addr in self.clist:
            conn = self.tcp.connect(addr)
            if not conn:
                conn = self.tcp.connect((addr[0], msg['srvport']))
            self.clist[addr] = conn
        self.addpeer(pid, self.clist[addr])
        self.peers[msg['id'][0]]['value'] = msg['id'][1]
        nmsg = self.mkmsg()
        nmsg['type'] = 'newpeer'
        nmsg['newlm'] = self.lace_max
        self.sendmsg(nmsg, pid)

    def handle_msg_newpeer(self, msg, addr, reply):
        '''
        addr is introducing itself as a new peer
        '''
        if not self.ispeer(msg['id'][1]):
            # something's broke
            return
        self.addpeer(msg['id'][0], self.clist[addr])
        self.peers[msg['id'][0]]['value'] = msg['id'][1]
        self.lace_max = tuple(msg['newlm'])
        # clean house
        rem = []
        for a in self.peers:
            if not self.ispeer(self.peers[a]['value']):
                rem.append(a)
        for r in rem:
            del self.peers[r]

    def handle_msg_welcome(self, msg, addr, reply):
        if self.value != (0, 0):
            print "what"
            return
        # we are new
        self.value = tuple(msg['value'])
        if self.value == (0, 0):
            # dafuq
            return
        self.clock = self.loadstamp(msg['itc'])
        # add whoever we're talking to as a temporary peer
        self.addpeer(msg['id'][0], self.clist[addr])
        self.peers[msg['id'][0]]['value'] = msg['id'][1]
        nmsg = self.mkmsg()
        nmsg['type'] = 'needpeer'
        self.broadcast(nmsg)

    def handle_msg_hello(self, msg, addr, reply):
        # we are being greeted
        nmsg = self.mkmsg()
        nmsg['type'] = 'welcome'
        nlm = self.get_next_addr(self.lace_max)
        nmsg['value'] = nlm
        a, b = self.clock.fork()
        self.clock = a
        nmsg['itc'] = self.dumpstamp(b)
        self.sendmsg_raw(nmsg, addr)
        # bump lace_max site-wide
        # XXX this is broken right now
        self.lace_max = nlm
        self.clock.event()
        bmsg = self.mkmsg('newlm')
        bmsg['newlm'] = self.lace_max
        self.broadcast(bmsg)

    def broadcast(self, msg):
        with self.lock:
            for peer in self.peers:
                self.sendmsg(msg, peer)
            self.sendmsg(msg, self.uuid) # sigh

    def sendmsg(self, msg, peerid):
        if self.uuid == peerid:
            if msg['type'] == 'maekawa':
                msg['stamp'] = uuid.uuid4().int # newstamp
                self.handle_msg(json.dumps(msg), None)
            return
        self.seen += msg['stamp']
        msg = json.dumps(msg)
        ct = self.peers[peerid]['contact']
        if ct:
            self.tcp.send(msg, ct)

    def sendmsg_raw(self, msg, addr):
        self.seen += msg['stamp']
        msg = json.dumps(msg)
        if not addr in self.clist:
            c = self.tcp.connect(addr)
            self.clist[addr] = c
        else:
            c = self.clist[addr]
        self.tcp.send(msg, c)
