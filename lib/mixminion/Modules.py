# Copyright 2002 Nick Mathewson.  See LICENSE for licensing information.
# $Id: Modules.py,v 1.22 2002/12/07 04:03:35 nickm Exp $

"""mixminion.Modules

   Type codes and dispatch functions for routing functionality."""

__all__ = [ 'ModuleManager', 'DeliveryModule',
	    'DROP_TYPE', 'FWD_TYPE', 'SWAP_FWD_TYPE',
	    'DELIVER_OK', 'DELIVER_FAIL_RETRY', 'DELIVER_FAIL_NORETRY',
	    'SMTP_TYPE', 'MBOX_TYPE' ]

import os
import re
import sys
import smtplib
import socket
import base64

import mixminion.Config
import mixminion.Packet
import mixminion.Queue
import mixminion.BuildMessage
from mixminion.Config import ConfigError, _parseBoolean, _parseCommand
from mixminion.Common import getLog, createPrivateDir, MixError

# Return values for processMessage
DELIVER_OK = 1
DELIVER_FAIL_RETRY = 2
DELIVER_FAIL_NORETRY = 3

# Numerically first exit type.
MIN_EXIT_TYPE  = 0x0100

# Mixminion types
DROP_TYPE      = 0x0000  # Drop the current message
FWD_TYPE       = 0x0001  # Forward the msg to an IPV4 addr via MMTP
SWAP_FWD_TYPE  = 0x0002  # SWAP, then forward the msg to an IPV4 addr via MMTP

# Exit types
SMTP_TYPE      = 0x0100  # Mail the message
MBOX_TYPE      = 0x0101  # Send the message to one of a fixed list of addresses

class DeliveryModule:
    """Abstract base for modules; delivery modules should implement
       the methods in this class.

       A delivery module has the following responsibilities:
           * It must have a 0-argument contructor.
           * If it is configurable, it must be able to specify its options,
             validate its configuration, and configure itself.
           * If it is advertisable, it must provide a server info block.
           * It must know its own name.
	   * It must know which types it handles.
	   * Of course, it needs to know how to deliver a message."""
    def __init__(self):
	"Zero-argument constructor, as required by Module protocol."
	pass

    def getConfigSyntax(self):
	"""Return a map from section names to section syntax, as described
	   in Config.py"""
        raise NotImplementedError("getConfigSyntax")

    def validateConfig(self, sections, entries, lines, contents):
	"""See mixminion.Config.validate"""
        pass

    def configure(self, config, manager):
	"""Configure this object using a given Config object, and (if
	   required) register it with the module manager."""
        raise NotImplementedError("configure")

    def getServerInfoBlock(self):
	"""Return a block for inclusion in a server descriptor."""
        raise NotImplementedError("getServerInfoBlock")

    def getName(self):
	"""Return the name of this module.  This name may be used to construct
	   directory paths, so it shouldn't contain any funny characters."""
        raise NotImplementedError("getName")

    def getExitTypes(self):
	"""Return a sequence of numeric exit types that this module can
           handle."""
        raise NotImplementedError("getExitTypes")

    def createDeliveryQueue(self, queueDir):
	"""Return a DeliveryQueue object suitable for delivering messages
	   via this module.  The default implementation returns a
	   SimpleModuleDeliveryQueue,  which (though adequate) doesn't
	   batch messages intended for the same destination.

	   For the 'address' component of the delivery queue, modules must
	   accept a tuple of: (exitType, address, tag).  If 'tag' is None,
	   the message has been decrypted; if 'tag' is 'err', the message is
	   corrupt.  Otherwise, the message is either a reply or an encrypted
	   forward message
	   """
        return SimpleModuleDeliveryQueue(self, queueDir)

    def processMessage(self, message, tag, exitType, exitInfo):
	"""Given a message with a given exitType and exitInfo, try to deliver
           it.  'tag' is as decribed in createDeliveryQueue. Return one of:
            DELIVER_OK (if the message was successfully delivered),
	    DELIVER_FAIL_RETRY (if the message wasn't delivered, but might be
              deliverable later), or
	    DELIVER_FAIL_NORETRY (if the message shouldn't be tried later)."""
        raise NotImplementedError("processMessage")


class ImmediateDeliveryQueue:
    """Helper class usable as delivery queue for modules that don't
       actually want a queue.  Such modules should have very speedy
       processMessage() methods, and should never have deliery fail."""
    def __init__(self, module):
	self.module = module

    def queueDeliveryMessage(self, (exitType, address, tag), message):
	try:
	    res = self.module.processMessage(message, tag, exitType, address)
	    if res == DELIVER_OK:
		return
	    elif res == DELIVER_FAIL_RETRY:
		getLog().error("Unable to retry delivery for message")
	    else:
		getLog().error("Unable to deliver message")
	except:
	    getLog().error_exc(sys.exc_info(),
			       "Exception delivering message")

    def sendReadyMessages(self):
	# We do nothing here; we already delivered the messages
	pass

class SimpleModuleDeliveryQueue(mixminion.Queue.DeliveryQueue):
    """Helper class used as a default delivery queue for modules that
       don't care about batching messages to like addresses."""
    def __init__(self, module, directory):
	mixminion.Queue.DeliveryQueue.__init__(self, directory)
	self.module = module

    def _deliverMessages(self, msgList):
	for handle, addr, message, n_retries in msgList:	
	    try:
		exitType, address, tag = addr
		result = self.module.processMessage(message,tag,exitType,address)
		if result == DELIVER_OK:
		    self.deliverySucceeded(handle)
		elif result == DELIVER_FAIL_RETRY:
		    self.deliveryFailed(handle, 1)
		else:
		    getLog().error("Unable to deliver message")
		    self.deliveryFailed(handle, 0)
	    except:
		getLog().error_exc(sys.exc_info(),
				   "Exception delivering message")
		self.deliveryFailed(handle, 0)

class ModuleManager:
    """A ModuleManager knows about all of the server modules in the system.

       A module may be in one of three states: unloaded, registered, or
       enabled.  An unloaded module is just a class in a python module.
       A registered module has been loaded, configured, and listed with
       the ModuleManager, but will not receive messags until it is
       enabled.

       Because modules need to tell the ServerConfig object aboutt their
       configuration options, initializing the ModuleManager is usually done
       through ServerConfig.  See ServerConfig.getModuleManager()."""
    ##
    # Fields
    #    syntax: extensions to the syntax configuration in Config.py
    #    modules: a list of DeliveryModule objects
    #    enabled: a set of enabled DeliveryModule objects
    #    nameToModule: Map from module name to module
    #    typeToModule: a map from delivery type to enabled deliverymodule.
    #    path: search path for python modules.
    #    queueRoot: directory where all the queues go.
    #    queues: a map from module name to queue (Queue objects must support
    #            queueMessage and sendReadyMessages as in DeliveryQueue.)

    def __init__(self):
	"Create a new ModuleManager"
        self.syntax = {}
        self.modules = []
        self.enabled = {}

	self.nameToModule = {}
        self.typeToModule = {}
	self.path = []
	self.queueRoot = None
	self.queues = {}

        self.registerModule(MBoxModule())
        self.registerModule(DropModule())
        self.registerModule(MixmasterSMTPModule())

    def _setQueueRoot(self, queueRoot):
	"""Sets a directory under which all modules' queue directories
	   should go."""
        self.queueRoot = queueRoot

    def getConfigSyntax(self):
	"""Returns a dict to extend the syntax configuration in a Config
	   object. Should be called after all modules are registered."""
        return self.syntax

    def registerModule(self, module):
	"""Inform this ModuleManager about a delivery module.  This method
   	   updates the syntax options, but does not enable the module."""
        getLog().info("Loading module %s", module.getName())
        self.modules.append(module)
        syn = module.getConfigSyntax()
        for sec, rules in syn.items():
            if self.syntax.has_key(sec):
                raise ConfigError("Multiple modules want to define [%s]"% sec)
        self.syntax.update(syn)
	self.nameToModule[module.getName()] = module

    def setPath(self, path):
	"""Sets the search path for Python modules"""
	if path:
	    self.path = path.split(":")
	else:
	    self.path = []

    def loadExtModule(self, className):
	"""Load and register a module from a python file.  Takes a classname
           of the format module.Class or package.module.Class.  Raises
	   MixError if the module can't be loaded."""
        ids = className.split(".")
        pyPkg = ".".join(ids[:-1])
        pyClassName = ids[-1]
	orig_path = sys.path[:]
	getLog().info("Loading module %s", className)
        try:
	    sys.path[0:0] = self.path
	    try:
		m = __import__(pyPkg, {}, {}, [pyClassName])
	    except ImportError, e:
		raise MixError("%s while importing %s" %(str(e),className))
        finally:
            sys.path = orig_path
	try:
	    pyClass = getattr(m, pyClassName)
	except AttributeError, e:
	    raise MixError("No class %s in module %s" %(pyClassName,pyPkg))
	try:
	    self.registerModule(pyClass())
	except Exception, e:
	    raise MixError("Error initializing module %s" %className)
	
    def validate(self, sections, entries, lines, contents):
        for m in self.modules:
            m.validateConfig(sections, entries, lines, contents)

    def configure(self, config):
	self._setQueueRoot(os.path.join(config['Server']['Homedir'],
					'work', 'queues', 'deliver'))
	createPrivateDir(self.queueRoot)
        for m in self.modules:
            m.configure(config, self)

    def enableModule(self, module):
	"""Sets up the module manager to deliver all messages whose exitTypes
            are returned by <module>.getExitTypes() to the module."""
        for t in module.getExitTypes():
            if (self.typeToModule.has_key(t) and
                self.typeToModule[t].getName() != module.getName()):
                getLog().warn("More than one module is enabled for type %x"%t)
            self.typeToModule[t] = module

        getLog().info("Module %s: enabled for types %s",
                      module.getName(),
                      map(hex, module.getExitTypes()))

	queueDir = os.path.join(self.queueRoot, module.getName())
	queue = module.createDeliveryQueue(queueDir)
	self.queues[module.getName()] = queue
        self.enabled[module.getName()] = 1

    def cleanQueues(self):
	for queue in self.queues.values():
	    queue.cleanQueue()
	
    def disableModule(self, module):
	"""Unmaps all the types for a module object."""
        getLog().info("Disabling module %s", module.getName())
        for t in module.getExitTypes():
            if (self.typeToModule.has_key(t) and
                self.typeToModule[t].getName() == module.getName()):
                del self.typeToModule[t]
	if self.queues.has_key(module.getName()):
	    del self.queues[module.getName()]
        if self.enabled.has_key(module.getName()):
            del self.enabled[module.getName()]

    def queueMessage(self, message, tag, exitType, address):
        mod = self.typeToModule.get(exitType, None)
        if mod is None:
            getLog().error("Unable to handle message with unknown type %s",
                           exitType)
	    return
	queue = self.queues[mod.getName()]
	getLog().debug("Delivering message %r (type %04x) via module %s",
		       message[:8], exitType, mod.getName())
	try:
	    payload = mixminion.BuildMessage.decodePayload(message, tag)
	except MixError, _:
	    queue.queueDeliveryMessage((exitType, address, 'err'), message)
	    return
	if payload is None:
	    # enrypted message
	    queue.queueDeliveryMessage((exitType, address, tag), message)
	else:
	    # forward message
	    queue.queueDeliveryMessage((exitType, address, None), payload)

    def sendReadyMessages(self):
	for name, queue in self.queues.items():
	    queue.sendReadyMessages()

    def getServerInfoBlocks(self):
        return [ m.getServerInfoBlock() for m in self.modules
                       if self.enabled.get(m.getName(),0) ]

#----------------------------------------------------------------------
class DropModule(DeliveryModule):
    """Null-object pattern: drops all messages it receives."""
    def getConfigSyntax(self):
        return { }
    def getServerInfoBlock(self):
        return ""
    def configure(self, config, manager):
	manager.enableModule(self)
    def getName(self):
        return "DROP"
    def getExitTypes(self):
        return [ DROP_TYPE ]
    def createDeliveryQueue(self, directory):
	return ImmediateDeliveryQueue(self)
    def processMessage(self, message, tag, exitType, exitInfo):
        getLog().debug("Dropping padding message")
        return DELIVER_OK

#----------------------------------------------------------------------
class MBoxModule(DeliveryModule):
    # FFFF This implementation can stall badly if we don't have a fast
    # FFFF local MTA.
    def __init__(self):
        DeliveryModule.__init__(self)
        self.enabled = 0
        self.addresses = {}

    def getConfigSyntax(self):
        return { "Delivery/MBOX" :
                 { 'Enabled' : ('REQUIRE',  _parseBoolean, "no"),
                   'AddressFile' : ('ALLOW', None, None),
                   'ReturnAddress' : ('ALLOW', None, None),
                   'RemoveContact' : ('ALLOW', None, None),
                   'SMTPServer' : ('ALLOW', None, 'localhost') }
                 }

    def validateConfig(self, sections, entries, lines, contents):
        # XXXX001 write this.  Parse address file.
        pass

    def configure(self, config, moduleManager):
        # XXXX001 Check this.  Conside error handling
	
        self.enabled = config['Delivery/MBOX'].get("Enabled", 0)
	if not self.enabled:
	    moduleManager.disableModule(self)
	    return

	self.server = config['Delivery/MBOX']['SMTPServer']
	self.addressFile = config['Delivery/MBOX']['AddressFile']
	self.returnAddress = config['Delivery/MBOX']['ReturnAddress']
	self.contact = config['Delivery/MBOX']['RemoveContact']
	if not self.addressFile:
	    raise ConfigError("Missing AddressFile field in Delivery/MBOX")
	if not self.returnAddress:
	    raise ConfigError("Missing ReturnAddress field in Delivery/MBOX")
	if not self.contact:
	    raise ConfigError("Missing RemoveContact field in Delivery/MBOX")

        self.nickname = config['Server']['Nickname']
        if not self.nickname:
            self.nickname = socket.gethostname()
        self.addr = config['Incoming/MMTP'].get('IP', "<Unknown host>")

	self.addresses = {}
        f = open(self.addressFile)
	address_line_re = re.compile(r'\s*([^\s:=]+)\s*[:=]\s*(\S+)')
	try:
	    lineno = 0
	    while 1:
                line = f.readline()
                if not line:
                    break
		line = line.strip()
		lineno += 1
		if line == '' or line[0] == '#':
		    continue
		m = address_line_re.match(line)
		if not m:
		    raise ConfigError("Bad address on line %s of %s"%(
			lineno,self.addressFile))
		self.addresses[m.group(1)] = m.group(2)
		getLog().trace("Mapping MBOX address %s -> %s", m.group(1),
			       m.group(2))
	finally:
	    f.close()

	moduleManager.enableModule(self)

    def getServerInfoBlock(self):
        return """\
                  [Delivery/MBOX]
                  Version: 0.1
               """

    def getName(self):
        return "MBOX"

    def getExitTypes(self):
        return [ MBOX_TYPE ]

    def processMessage(self, message, tag, exitType, address):
        assert exitType == MBOX_TYPE
        getLog().trace("Received MBOX message")
        info = mixminion.Packet.parseMBOXInfo(address)
	try:
	    address = self.addresses[info.user]
	except KeyError, _:
            getLog().error("Unknown MBOX user %r", info.user)
	    return DELIVER_FAIL_NORETRY

        msg = _escapeMessageForEmail(message, tag)

        fields = { 'user': address,
                   'return': self.returnAddress,
                   'nickname': self.nickname,
                   'addr': self.addr,
                   'contact': self.contact,
                   'msg': msg }
        msg = """\
To: %(user)s
From: %(return)s
Subject: Anonymous Mixminion message

THIS IS AN ANONYMOUS MESSAGE.  The mixminion server '%(nickname)s' at
%(addr)s has been configured to deliver messages to your address.  
If you do not want to receive messages in the future, contact %(contact)s 
and you will be removed.

%(msg)s""" % fields

        return sendSMTPMessage(self.server, [address], self.returnAddress, msg)

#----------------------------------------------------------------------
class SMTPModule(DeliveryModule):
    """Placeholder for real exit node implementation.
        DOCDOC document me."""
    def __init__(self):
        DeliveryModule.__init__(self)
        self.enabled = 0
    def getServerInfoBlock(self):
        if self.enabled:
            return "[Delivery/SMTP]\nVersion: 0.1\n"
        else:
            return ""
    def getName(self):
        return "SMTP"
    def getExitTypes(self):
        return (SMTP_TYPE,)

class MixmasterSMTPModule(SMTPModule):
    """Implements SMTP by relaying messages via Mixmaster nodes.  This
       is kind of unreliable and kludgey, but it does allow us to
       test mixminion by usingg Mixmaster nodes as exits."""
    # FFFF Mixmaster has tons of options.  Maybe we should use 'em...
    # FFFF ... or maybe we should deliberately ignore them, since
    # FFFF this is only a temporary workaround until enough people 
    # FFFF are running SMTP exit nodes
    def __init__(self):
        SMTPModule.__init__(self)
        self.mixDir = None
    def getConfigSyntax(self):
        return { "Delivery/SMTP-Via-Mixmaster" :
                 { 'Enabled' : ('REQUIRE', _parseBoolean, "no"),
                   'MixCommand' : ('REQUIRE', _parseCommand, None),
                   'Server' : ('REQUIRE', None, None),
                   'SubjectLine' : ('ALLOW', None,
                                    'Type-III Anonymous Message'),
                   }
                 }
                   
    def validateConfig(self, sections, entries, lines, contents):
        #FFFF001 implement
        pass
    def configure(self, config, manager):
        sec = config['Delivery/SMTP-Via-Mixmaster']
        self.enabled = sec.get("Enabled", 0)
        if not self.enabled:
            manager.disableModule(self)
            return
        cmd = sec['MixCommand']
        self.server = sec['Server']
        self.subject = sec['SubjectLine']
        self.command = cmd[0]
        self.options = tuple(cmd[1]) + ("-l", self.server,
					"-s", self.subject)
        manager.enableModule(self)

    def getName(self): 
        return "SMTP_MIX2" 
    def createDeliveryQueue(self, queueDir):
        self.tmpQueue = mixminion.Queue.Queue(queueDir+"_tmp", 1, 1)
        self.tmpQueue.removeAll()
        return _MixmasterSMTPModuleDeliveryQueue(self, queueDir)
    def processMessage(self, message, tag, exitType, smtpAddress):
        assert exitType == SMTP_TYPE
	# parseSMTPInfo will raise a parse error if the mailbox is invalid.
        info = mixminion.Packet.parseSMTPInfo(smtpAddress)

        msg = _escapeMessageForEmail(message, tag)
        handle = self.tmpQueue.queueMessage(msg)

        cmd = self.command
        opts = self.options + ("-t", info.email,
                               self.tmpQueue.getMessagePath(handle))
        code = os.spawnl(os.P_WAIT, cmd, cmd, *opts)
        getLog().debug("Queued Mixmaster message: exit code %s", code)
        self.tmpQueue.removeMessage(handle)
        return DELIVER_OK
                         
    def flushMixmasterPool(self):
        "DOCDOC"
        cmd = self.command
        getLog().debug("Flushing Mixmaster pool")
        os.spawnl(os.P_NOWAIT, cmd, cmd, "-S")

class _MixmasterSMTPModuleDeliveryQueue(SimpleModuleDeliveryQueue):
    "DOCDOC"
    def __init__(self, module, directory):
        SimpleModuleDeliveryQueue.__init__(self, module, directory)
    def _deliverMessages(self, msgList):
        SimpleModuleDeliveryQueue._deliverMessages(self, msgList)
        self.module.flushMixmasterPool()
        
#----------------------------------------------------------------------
def sendSMTPMessage(server, toList, fromAddr, message):
    getLog().trace("Sending message via SMTP host %s to %s", server, toList)
    con = smtplib.SMTP(server)
    try:
	con.sendmail(fromAddr, toList, message)
	res = DELIVER_OK
    except smtplib.SMTPException, e:
	getLog().warn("Unsuccessful smtp: "+str(e))
	res = DELIVER_FAIL_RETRY

    con.quit()
    con.close()

    return res

#----------------------------------------------------------------------

# DOCDOC
_allChars = "".join(map(chr, range(256)))
# DOCDOC
# ????001 Are there any nonprinting chars >= 0x7f to worry about now?
_nonprinting = "".join(map(chr, range(0x00, 0x07)+range(0x0E, 0x20)))
def isPrintable(s):
    """Return true iff s consists only of printable characters."""
    printable = s.translate(_allChars, _nonprinting)
    return len(printable) == len(s)

def _escapeMessageForEmail(msg, tag):
    """DOCDOC
         -> None | str """
    m = _escapeMessage(msg, tag, text=1)
    if m is None:
	return None
    code, msg, tag = m

    if code == 'ENC':
	junk_msg = """\
This message is not in plaintext.  It's either 1) a reply; 2) a forward
message encrypted to you; or 3) junk.\n\n"""
    else:
	junk_msg = ""

    if tag is not None:
	tag = "Decoding handle: "+tag+"\n"
    else:
	tag = ""

    return """\
%s============ ANONYMOUS MESSAGE BEGINS
%s%s============ ANONYMOUS MESSAGE ENDS\n""" %(junk_msg, tag, msg)

def _escapeMessage(message, tag, text=0):
    """DOCDOC
	 (message,tag|None,output-as-text?) 
                -> ("TXT"|"BIN"|"ENC", message, tag|None) or None
    """
    if tag == 'err':
	return None
    elif tag is not None:
	code = "ENC"
    else:
	tag = None
	if isPrintable(message):
	    code = "TXT"
	else:
	    code = "BIN"

    if text and (code != "TXT") :
	message = base64.encodestring(message)
    if text and tag:
	tag = base64.encodestring(tag).strip()

    return code, message, tag
