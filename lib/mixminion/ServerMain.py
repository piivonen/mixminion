# Copyright 2002 Nick Mathewson.  See LICENSE for licensing information.
# $Id: ServerMain.py,v 1.17 2002/12/07 04:03:35 nickm Exp $

"""mixminion.ServerMain

   The main loop and related functionality for a Mixminion server.
   See the "MixminionServer" class for more information about how it
   all works.

   BUG: No support for encrypting private keys."""

import os
import getopt
import sys
import time
import bisect

import mixminion._minionlib
import mixminion.Crypto
import mixminion.Queue
import mixminion.MMTPServer
from mixminion.ServerInfo import ServerKeyset, ServerInfo, _date, _time, \
     generateServerDescriptorAndKeys
from mixminion.Common import getLog, MixFatalError, MixError, secureDelete, \
     createPrivateDir, previousMidnight, ceilDiv

# Directory layout:
#     MINION_HOME/work/queues/incoming/
#                             mix/
#                             outgoing/
#                             deliver/mbox/
#                      tls/dhparam
#                      hashlogs/hash_1 ...
#                 log
#                 keys/identity.key
#                      key_1/ServerDesc
#                            mix.key
#                            mmtp.key
#                            mmtp.cert
#                      key_2/...
#                 conf/miniond.conf
#                       ....

class ServerKeyring:
    """A ServerKeyRing remembers current and future keys, descriptors, and
       hash logs for a mixminion server.

       FFFF We need a way to generate keys as needed, not just a month's
       FFFF worth of keys up front. 
       """
    # homeDir: server home directory
    # keyDir: server key directory
    # keySloppiness: fudge-factor: how forgiving are we about key liveness?
    # keyIntervals: list of (start, end, keyset Name)
    # liveKey: list of (start, end, keyset name for current key.)
    # nextRotation: time_t when this key expires.
    # keyRange: tuple of (firstKey, lastKey) to represent which key names
    #      have keys on disk.

    def __init__(self, config):
	"Create a ServerKeyring from a config object"
	self.configure(config)

    def configure(self, config):
	"Set up a SeverKeyring from a config object"
	self.config = config
	self.homeDir = config['Server']['Homedir']
	self.keyDir = os.path.join(self.homeDir, 'keys')
	self.hashDir = os.path.join(self.homeDir, 'work', 'hashlogs')
	self.keySloppiness = config['Server']['PublicKeySloppiness'][2]
	self.checkKeys()

    def checkKeys(self):
	"""Internal method: read information about all this server's
	   currently-prepared keys from disk."""
        self.keyIntervals = []
	firstKey = sys.maxint
	lastKey = 0

	getLog().debug("Scanning server keystore at %s", self.keyDir)

	if not os.path.exists(self.keyDir):
	    getLog().info("Creating server keystore at %s", self.keyDir)
	    createPrivateDir(self.keyDir)

        for dirname in os.listdir(self.keyDir):
	    if not os.path.isdir(os.path.join(self.keyDir,dirname)):
		continue
            if not dirname.startswith('key_'):
		getLog().warn("Unexpected directory %s under %s",
			      dirname, self.keyDir)
                continue
            keysetname = dirname[4:]
	    try:
		setNum = int(keysetname)
		if setNum < firstKey: firstKey = setNum
		if setNum > lastKey: lastKey = setNum
	    except ValueError, _:
		getLog().warn("Unexpected directory %s under %s",
			      dirname, self.keyDir)
		continue

            d = os.path.join(self.keyDir, dirname)
            si = os.path.join(d, "ServerDesc")
            if os.path.exists(si):
                inf = ServerInfo(fname=si, assumeValid=1)
                t1 = inf['Server']['Valid-After']
                t2 = inf['Server']['Valid-Until']
                self.keyIntervals.append( (t1, t2, keysetname) )
		getLog().debug("Found key %s (valid from %s to %s)",
			       dirname, _date(t1), _date(t2))
	    else:
		getLog().warn("No server descriptor found for key %s"%dirname)

        self.keyIntervals.sort()
	self.keyRange = (firstKey, lastKey)

	# Now we try to see whether we have more or less than 1 key in effect
	# for a given time.
	for idx in xrange(len(self.keyIntervals)-1):
	    end = self.keyIntervals[idx][1]
	    start = self.keyIntervals[idx+1][0]
	    if start < end:
		getLog().warn("Multiple keys for %s.  That's unsupported.",
			      _date(end))
	    elif start > end:
		getLog().warn("Gap in key schedule: no key from %s to %s",
			      _date(end), _date(start))

	self.nextKeyRotation = 0 # Make sure that now > nextKeyRotation before
	                         # we call _getLiveKey()
	self._getLiveKey()       # Set up liveKey, nextKeyRotation.

    def getIdentityKey(self):
	"""Return this server's identity key.  Generate one if it doesn't
	   exist."""
        # FFFF Use this, somehow.
	password = None
	fn = os.path.join(self.keyDir, "identity.key")
	bits = self.config['Server']['IdentityKeyBits']
	if os.path.exists(fn):
	    key = mixminion.Crypto.pk_PEM_load(fn, password)
	    keylen = key.get_modulus_bytes()*8
	    if keylen != bits:
		getLog().warn(
		    "Stored identity key has %s bits, but you asked for %s.",
		    keylen, bits)
	else:
	    getLog().info("Generating identity key. (This may take a while.)")
	    key = mixminion.Crypto.pk_generate(bits)
	    mixminion.Crypto.pk_PEM_save(key, fn, password)
	    getLog().info("Generated %s-bit identity key.", bits)

	return key

    def removeIdentityKey(self):
        """Remove this server's identity key."""
        fn = os.path.join(self.keyDir, "identity.key")
        if not os.path.exists(fn):
            getLog().info("No identity key to remove.")
        else:
            getLog().warn("Removing identity key in 10 seconds")
            time.sleep(10)
            getLog().warn("Removing identity key")
            secureDelete([fn], blocking=1)

	dhfile = os.path.join(self.homeDir, 'work', 'tls', 'dhparam')
        if os.path.exists('dhfile'):
            getLog().info("Removing diffie-helman parameters file")
            secureDelete([dhfile], blocking=1)

    def createKeys(self, num=1, startAt=None):
	"""Generate 'num' public keys for this server. If startAt is provided,
           make the first key become valid at'startAt'.  Otherwise, make the
	   first key become valid right after the last key we currently have
	   expires.  If we have no keys now, make the first key start now."""
        # FFFF Use this.
	#password = None

	if startAt is None:
	    if self.keyIntervals:
		startAt = self.keyIntervals[-1][1]+60
	    else:
		startAt = time.time()+60

	startAt = previousMidnight(startAt)

	firstKey, lastKey = self.keyRange

	for _ in xrange(num):
	    if firstKey == sys.maxint:
		keynum = firstKey = lastKey = 1
	    elif firstKey > 1:
		firstKey -= 1
		keynum = firstKey
	    else:
		lastKey += 1
		keynum = lastKey

	    keyname = "%04d" % keynum

	    nextStart = startAt + self.config['Server']['PublicKeyLifetime'][2]

	    getLog().info("Generating key %s to run from %s through %s (GMT)",
			  keyname, _date(startAt), _date(nextStart-3600))
 	    generateServerDescriptorAndKeys(config=self.config,
					    identityKey=self.getIdentityKey(),
					    keyname=keyname,
					    keydir=self.keyDir,
					    hashdir=self.hashDir,
					    validAt=startAt)
	    startAt = nextStart

        self.checkKeys()

    def removeDeadKeys(self, now=None):
	"""Remove all keys that have expired"""
        self.checkKeys()

        if now is None:
            now = time.time()
            expiryStr = " expired"
        else:
            expiryStr = ""

        cutoff = now - self.keySloppiness
	dirs = [ os.path.join(self.keyDir,"key_"+name)
                  for va, vu, name in self.keyIntervals if vu < cutoff ]

	for dirname, (va, vu, name) in zip(dirs, self.keyIntervals):
            getLog().info("Removing%s key %s (valid from %s through %s)",
                        expiryStr, name, _date(va), _date(vu-3600))
	    files = [ os.path.join(dirname,f)
                                 for f in os.listdir(dirname) ]
	    secureDelete(files, blocking=1)
	    os.rmdir(dirname)

	self.checkKeys()

    def _getLiveKey(self, when=None):
	"""Find the first key that is now valid.  Return (Valid-after,
	   valid-util, name)."""
        if not self.keyIntervals:
	    self.liveKey = None
	    self.nextKeyRotation = 0
	    return None

	w = when
	if when is None:
	    when = time.time()
	    if when < self.nextKeyRotation:
		return self.liveKey

	idx = bisect.bisect(self.keyIntervals, (when, None, None))-1
	k = self.keyIntervals[idx]
	if w is None:
	    self.liveKey = k
	    self.nextKeyRotation = k[1]

	return k

    def getNextKeyRotation(self):
	"""Return the expiration time of the current key"""
        return self.nextKeyRotation

    def getServerKeyset(self):
	"""Return a ServerKeyset object for the currently live key."""
	# FFFF Support passwords on keys
	_, _, name = self._getLiveKey()
	keyset = ServerKeyset(self.keyDir, name, self.hashDir)
	keyset.load()
	return keyset

    def getDHFile(self):
	"""Return the filename for the diffie-helman parameters for the
	   server.  Creates the file if it doesn't yet exist."""
	dhdir = os.path.join(self.homeDir, 'work', 'tls')
	createPrivateDir(dhdir)
	dhfile = os.path.join(dhdir, 'dhparam')
        if not os.path.exists(dhfile):
            getLog().info("Generating Diffie-Helman parameters for TLS...")
            mixminion._minionlib.generate_dh_parameters(dhfile, verbose=0)
            getLog().info("...done")
	else:
	    getLog().debug("Using existing Diffie-Helman parameter from %s",
			   dhfile)

        return dhfile

    def getTLSContext(self):
	"""Create and return a TLS context from the currently live key."""
        keys = self.getServerKeyset()
        return mixminion._minionlib.TLSContext_new(keys.getCertFileName(),
						   keys.getMMTPKey(),
						   self.getDHFile())

    def getPacketHandler(self):
	"""Create and return a PacketHandler from the currently live key."""
        keys = self.getServerKeyset()
	packetKey = keys.getPacketKey()
	hashlog = mixminion.HashLog.HashLog(keys.getHashLogFileName(),
						 keys.getMMTPKeyID())
        return mixminion.PacketHandler.PacketHandler(packetKey,
						     hashlog)

class IncomingQueue(mixminion.Queue.DeliveryQueue):
    """A DeliveryQueue to accept messages from incoming MMTP connections,
       process them with a packet handler, and send them into a mix pool."""

    def __init__(self, location, packetHandler):
	"""Create an IncomingQueue that stores its messages in <location>
	   and processes them through <packetHandler>."""
	mixminion.Queue.DeliveryQueue.__init__(self, location)
	self.packetHandler = packetHandler
	self.mixPool = None

    def connectQueues(self, mixPool):
	"""Sets the target mix queue"""
	self.mixPool = mixPool

    def queueMessage(self, msg):
	"""Add a message for delivery"""
	getLog().trace("Inserted message %r into incoming queue", msg[:8])
	self.queueDeliveryMessage(None, msg)

    def _deliverMessages(self, msgList):
	"Implementation of abstract method from DeliveryQueue."
	ph = self.packetHandler
	for handle, _, message, n_retries in msgList:
	    try:
		res = ph.processMessage(message)
		if res is None:
		    # Drop padding before it gets to the mix.
		    getLog().debug("Padding message %r dropped", 
				   message[:8])
		else:
		    getLog().debug("Processed message %r; inserting into pool",
				   message[:8])
		    self.mixPool.queueObject(res)
		    self.deliverySucceeded(handle)
	    except mixminion.Crypto.CryptoError, e:
		getLog().warn("Invalid PK or misencrypted packet header: %s",
			      e)
		self.deliveryFailed(handle)
	    except mixminion.Packet.ParseError, e:
		getLog().warn("Malformed message dropped: %s", e)
		self.deliveryFailed(handle)
	    except mixminion.PacketHandler.ContentError, e:
		getLog().warn("Discarding bad packet: %s", e)
		self.deliveryFailed(handle)

class MixPool:
    """Wraps a mixminion.Queue.*MixQueue to send messages to an exit queue
       and a delivery queue."""
    def __init__(self, queue):
	"""Create a new MixPool to wrap a given *MixQueue."""
	self.queue = queue
	self.outgoingQueue = None
	self.moduleManager = None

    def queueObject(self, obj):
	"""Insert an object into the queue."""
	self.queue.queueObject(obj)

    def count(self):
	"Return the number of messages in the queue"
	return self.queue.count()

    def connectQueues(self, outgoing, manager):
	"""Sets the queue for outgoing mixminion packets, and the
  	   module manager for deliverable messages."""
	self.outgoingQueue = outgoing
	self.moduleManager = manager

    def mix(self):
	"""Get a batch of messages, and queue them for delivery as
	   appropriate."""
	handles = self.queue.getBatch()
	getLog().debug("Mixing %s messages out of %s", 
		       len(handles), self.queue.count())
	for h in handles:
	    tp, info = self.queue.getObject(h)
	    if tp == 'EXIT':
		rt, ri, app_key, tag, payload = info
		getLog().debug("  (sending message %r to exit modules)", 
			       payload[:8])
		self.moduleManager.queueMessage(payload, tag, rt, ri)
	    else:
		assert tp == 'QUEUE'
		ipv4, msg = info
		getLog().debug("  (sending message %r to MMTP server)", 
			       msg[:8])
		self.outgoingQueue.queueDeliveryMessage(ipv4, msg)
	    self.queue.removeMessage(h)

class OutgoingQueue(mixminion.Queue.DeliveryQueue):
    """DeliveryQueue to send messages via outgoing MMTP connections."""
    def __init__(self, location):
	"""Create a new OutgoingQueue that stores its messages in a given
 	   location."""
        mixminion.Queue.DeliveryQueue.__init__(self, location)
	self.server = None

    def connectQueues(self, server):
	"""Set the MMTPServer that this OutgoingQueue informs of its
	   deliverable messages."""
	self.server = server

    def _deliverMessages(self, msgList):
	"Implementation of abstract method from DeliveryQueue."
	# Map from addr -> [ (handle, msg) ... ]
	msgs = {}
	for handle, addr, message, n_retries in msgList:
	    msgs.setdefault(addr, []).append( (handle, message) )
	for addr, messages in msgs.items():
	    handles, messages = zip(*messages)
	    self.server.sendMessages(addr.ip, addr.port, addr.keyinfo,
				     list(messages), list(handles))

class _MMTPServer(mixminion.MMTPServer.MMTPServer):
    """Implementation of mixminion.MMTPServer that knows about
       delivery queues."""
    def __init__(self, config, tls):
        mixminion.MMTPServer.MMTPServer.__init__(self, config, tls)

    def connectQueues(self, incoming, outgoing):
        self.incomingQueue = incoming
        self.outgoingQueue = outgoing

    def onMessageReceived(self, msg):
        self.incomingQueue.queueMessage(msg)

    def onMessageSent(self, msg, handle):
        self.outgoingQueue.deliverySucceeded(handle)

    def onMessageUndeliverable(self, msg, handle, retriable):
	self.outgoingQueue.deliveryFailed(handle, retriable)

class MixminionServer:
    """Wraps and drives all the queues, and the async net server.  Handles
       all timed events."""
    def __init__(self, config):
	"""Create a new server from a ServerConfig."""
	getLog().debug("Initializing server")
	self.config = config
	self.keyring = ServerKeyring(config)
	if self.keyring._getLiveKey() is None:
	    getLog().info("Generating a month's worth of keys.")
	    getLog().info("(Don't count on this feature in future versions.)")
	    # We might not be able to do this, if we password-encrypt keys
	    keylife = config['Server']['PublicKeyLifetime'][2]
	    nKeys = ceilDiv(30*24*60*60, keylife)
	    self.keyring.createKeys(nKeys)

	getLog().trace("Initializing packet handler")
	self.packetHandler = self.keyring.getPacketHandler()
	getLog().trace("Initializing TLS context")
	tlsContext = self.keyring.getTLSContext()
	getLog().trace("Initializing MMTP server")
	self.mmtpServer = _MMTPServer(config, tlsContext)

	# FFFF Modulemanager should know about async so it can patch in if it
	# FFFF needs to.
	getLog().trace("Initializing delivery module")
	self.moduleManager = config.getModuleManager()
	self.moduleManager.configure(config)

	homeDir = config['Server']['Homedir']
	queueDir = os.path.join(homeDir, 'work', 'queues')

	incomingDir = os.path.join(queueDir, "incoming")
	getLog().trace("Initializing incoming queue")
	self.incomingQueue = IncomingQueue(incomingDir, self.packetHandler)
	getLog().trace("Found %d pending messages in incoming queue", 
		       self.incomingQueue.count())

	mixDir = os.path.join(queueDir, "mix")
	# FFFF The choice of mix algorithm should be configurable
	getLog().trace("Initializing Mix pool")
	self.mixPool = MixPool(mixminion.Queue.TimedMixQueue(mixDir, 60))
	getLog().trace("Found %d pending messages in Mix pool",
		       self.mixPool.count())

	outgoingDir = os.path.join(queueDir, "outgoing")
	getLog().trace("Initializing outgoing queue")
	self.outgoingQueue = OutgoingQueue(outgoingDir)
	getLog().trace("Found %d pending messages in outgoing queue",
		       self.outgoingQueue.count())

	getLog().trace("Connecting queues")
	self.incomingQueue.connectQueues(mixPool=self.mixPool)
	self.mixPool.connectQueues(outgoing=self.outgoingQueue,
				   manager=self.moduleManager)
	self.outgoingQueue.connectQueues(server=self.mmtpServer)
	self.mmtpServer.connectQueues(incoming=self.incomingQueue,
				      outgoing=self.outgoingQueue)

    def run(self):
	"""Run the server; don't return unless we hit an exception."""
	# FFFF Use heapq to schedule events? [I don't think so; there are only
	# FFFF   two events, after all!]
	now = time.time()
	MIX_INTERVAL = 20  # FFFF Configurable!
	nextMix = now + MIX_INTERVAL
	nextShred = now + 6000
	#FFFF Unused
	#nextRotate = self.keyring.getNextKeyRotation()
	while 1:
	    getLog().trace("Next mix at %s", _time(nextMix))
	    while time.time() < nextMix:
		# Handle pending network events
		self.mmtpServer.process(1)
		# Process any new messages that have come in, placing them
		# into the mix pool.
		self.incomingQueue.sendReadyMessages()

	    # Before we mix, we need to log the hashes to avoid replays.
	    # FFFF We need to recover on server failure.
	    self.packetHandler.syncLogs()

	    getLog().trace("Mix interval elapsed")
	    # Choose a set of outgoing messages; put them in outgoingqueue and
	    # modulemanger
	    self.mixPool.mix()
	    # Send outgoing messages
	    self.outgoingQueue.sendReadyMessages()
	    # Send exit messages
	    self.moduleManager.sendReadyMessages()

	    # Choose next mix interval
	    now = time.time()
	    nextMix = now + MIX_INTERVAL

	    if now > nextShred:
		# FFFF Configurable shred interval
		getLog().trace("Expunging deleted messages from queues")
		self.incomingQueue.cleanQueue()
		self.mixPool.queue.cleanQueue()
		self.outgoingQueue.cleanQueue()
		self.moduleManager.cleanQueues()
		nextShred = now + 6000

    def close(self):
	"""Release all resources; close all files."""
	self.packetHandler.close()

#----------------------------------------------------------------------

def usageAndExit(cmd):
    executable = sys.argv[0]
    print >>sys.stderr, "Usage: %s %s [-h] [-f configfile]" % (executable, cmd)
    sys.exit(0)

def configFromServerArgs(cmd, args):
    options, args = getopt.getopt(args, "hf:", ["help", "config="])
    if args:
	usageAndExit(cmd)
    configFile = "/etc/mixminiond.conf"
    for o,v in options:
	if o in ('-h', '--help'):
	    usageAndExit(cmd)
	if o in ('-f', '--config'):
	    configFile = v

    return readConfigFile(configFile)

def readConfigFile(configFile):
    try:
	return mixminion.Config.ServerConfig(fname=configFile)
    except (IOError, OSError), e:
	print >>sys.stderr, "Error reading configuration file %r:"%configFile
	print >>sys.stderr, "   ", str(e)
	sys.exit(1)
    except mixminion.Config.ConfigError, e:
	print >>sys.stderr, "Error in configuration file %r"%configFile
	print >>sys.stderr, str(e)
	sys.exit(1)
    return None #suppress pychecker warning

#----------------------------------------------------------------------
def runServer(cmd, args):
    config = configFromServerArgs(cmd, args)
    try:
	mixminion.Common.getLog().configure(config)
	getLog().debug("Configuring server")
	mixminion.Common.configureShredCommand(config)
	mixminion.Crypto.init_crypto(config)

	server = MixminionServer(config)
    except:
	getLog().fatal_exc(sys.exc_info(),"Exception while configuring server")
	print >>sys.stderr, "Shutting down because of exception"
        #XXXX print stack trace as well as logging?
	sys.exit(1)

    getLog().info("Starting server")
    try:
	server.run()
    except KeyboardInterrupt:
	pass
    except:
	getLog().fatal_exc(sys.exc_info(),"Exception while running server")
        #XXXX print stack trace as well as logging?
    getLog().info("Server shutting down")
    server.close()
    getLog().info("Server is shut down")

    sys.exit(0)

#----------------------------------------------------------------------
def runKeygen(cmd, args):
    options, args = getopt.getopt(args, "hf:n:",
                                  ["help", "config=", "keys="])
    # FFFF password-encrypted keys
    # FFFF Ability to fill gaps
    # FFFF Ability to generate keys with particular start/end intervals
    keys=1
    usage=0
    configFile = '/etc/miniond.conf'
    for opt,val in options:
	if opt in ('-h', '--help'):
	    usage=1
	elif opt in ('-f', '--config'):
	    configFile = val
	elif opt in ('-n', '--keys'):
	    try:
		keys = int(val)
	    except ValueError, _:
		print >>sys.stderr,("%s requires an integer" %opt)
		usage = 1
    if usage:
        print >>sys.stderr, "Usage: %s [-h] [-f configfile] [-n nKeys]"%cmd
        sys.exit(1)

    config = readConfigFile(configFile)

    getLog().setMinSeverity("INFO")
    mixminion.Crypto.init_crypto(config)
    keyring = ServerKeyring(config)
    print >>sys.stderr, "Creating %s keys..." % keys
    for i in xrange(keys):
	keyring.createKeys(1)
	print >> sys.stderr, ".... (%s/%s done)" % (i+1,keys)

#----------------------------------------------------------------------
def removeKeys(cmd, args):
    # FFFF Resist removing keys that have been published.
    # FFFF Generate 'suicide note' for removing identity key.
    options, args = getopt.getopt(args, "hf:", ["help", "config=",
                                                "remove-identity"])
    if args:
        print >>sys.stderr, "%s takes no arguments"%cmd
        usage = 1
        args = options = ()
    usage = 0
    removeIdentity = 0
    configFile = '/etc/miniond.conf'
    for opt,val in options:
	if opt in ('-h', '--help'):
	    usage=1
	elif opt in ('-f', '--config'):
	    configFile = val
	elif opt == '--remove-identity':
            removeIdentity = 1
    if usage:
        print >>sys.stderr, \
              "Usage: %s [-h|--help] [-f configfile] [--remove-identity]"%cmd
        sys.exit(1)

    config = readConfigFile(configFile)
    mixminion.Common.configureShredCommand(config)
    getLog().setMinSeverity("INFO")
    keyring = ServerKeyring(config)
    keyring.checkKeys()
    # This is impossibly far in the future.
    keyring.removeDeadKeys(now=(1L << 36))
    if removeIdentity:
        keyring.removeIdentityKey()
    getLog().info("Done removing keys")
