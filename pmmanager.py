#!/usr/bin/env python3
# -*- Mode: python; tab-width: 4; indent-tabs-mode: nil; c-basic-offset: 4 -*-
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation; either version 2 of the License, or (at your option) any
# later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License along
# with this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright (C) 2014 Aleksander Morgado <aleksander@aleksander.es>
#

import os, sys, gi, pexpect, time,logging, random, subprocess, datetime, signal

gi.require_version('ModemManager', '1.0')
from gi.repository import GLib, GObject, Gio, ModemManager

"""
The ModemWatcher class is responsible for monitoring ModemManager
"""
def append_content(file,data):
	f = open(file, 'a')
	f.write(data)
	f.close()
def append_resolv(ip):
	f = open('/etc/resolv.conf', 'a')
	f.write("nameserver "+ip+"\n")
	f.close()
def shexcute(cmd,prefix=""):
	r=pexpect.spawn(cmd)
	r.expect(pexpect.EOF)
	val=str(r.before).strip()
	return val
def creatfile(fname,content):
	f = open(fname, 'w')
	f.write(content)
def ifaceipremover(lan):
	try:
		os.system('sudo ip addr flush ' + lan)
	except Exception:
		logging.debug('Failed: sudo ip addr flush ' + lan)
	try:
		os.system('sudo ip route flush table ' + lan)
	except Exception:
		logging.debug('Failed: sudo ip route flush table ' + lan)
	try:
		os.system('sudo ip rule del table ' + lan)
	except Exception:
		logging.debug('Failed: sudo ip rule del table ' + lan)
	try:
		os.system('sudo ifconfig ' + lan + ' down')
	except Exception:
		logging.debug('Failed: sudo ifconfig ' + lan + ' down')


#os.system("sudo ip rule flush cache")
from netaddr import *
def file_content(file):
	f = open(file, 'r')
	fc = f.read()
	fc = fc.strip()
	f.close()
	return fc
nameservers=list()
for line in file_content("/etc/resolv.conf").split('\n'):
	if line.startswith("nameserver"):
		nameservers.append(line[line.find(" ")+1:])
class mmanager:
	def __init__(self,q):
		# Flag for initial logs
		self.initializing = True
		self.q = q
		self.modems=list()
		# Setup DBus monitoring
		self.connection = Gio.bus_get_sync (Gio.BusType.SYSTEM, None)
		self.manager = ModemManager.Manager.new_sync (self.connection,
													  Gio.DBusObjectManagerClientFlags.DO_NOT_AUTO_START,
													  None)
		# IDs for added/removed signals
		self.object_added_id = 0
		self.object_removed_id = 0
		# Follow availability of the ModemManager process
		self.available = False
		self.manager.connect('notify::name-owner', self.on_name_owner)

		self.on_name_owner(self.manager, None)
		# Finish initialization
		self.initializing = False


	"""
	ModemManager is now available
	"""
	def set_available(self):
		if self.available == False or self.initializing == True:
			print('[ModemWatcher] ModemManager service is available in bus')
		self.object_added_id = self.manager.connect('object-added', self.on_object_added)
		self.object_removed_id = self.manager.connect('object-removed', self.on_object_removed)
		self.available = True
		# Initial scan
		if self.initializing == True:
			for obj in self.manager.get_objects():
				self.on_object_added(self.manager, obj)

	def cprint(self, index, action, result):
		# sys.stdout.flush()
		self.q.put_nowait("MODEM:" + str(index) + ":" + action + ":" + result)

	"""
	ModemManager is now unavailable
	"""
	def set_unavailable(self):
		if self.available == True or self.initializing == True:
			print('[ModemWatcher] ModemManager service not available in bus')
		if self.object_added_id:
			self.manager.disconnect(self.object_added_id)
			self.object_added_id = 0
		if self.object_removed_id:
			self.manager.disconnect(self.object_removed_id)
			self.object_removed_id = 0
		self.available = False
		os.system('sudo ModemManager &')
	def on_name_owner(self, manager, prop):
		if self.manager.get_name_owner():
			self.set_available()
		else:
			self.set_unavailable()



	def on_object_added(self, manager, obj):
		found = False
		path = str(obj.get_object_path())
		mindex = int(path[path.rfind("/") + 1:])
		for m in self.modems:
			if mindex == m.mindex:
				m.initessentials(obj)
				found = True
		if found == False:
			self.modems.append(pmodem(obj, self.q))
	def on_object_removed(self, manager, obj):
		path = str(obj.get_object_path())
		mindex = int(path[path.rfind("/") + 1:])
		for m in self.modems:
			if mindex==m.mindex:
				m.remove()
class pmodem:
	def __init__(self, obj,q):
		self.q = q

		self.checked_internet = False
		self.have_internet = False
		self.bearer = None
		self.status= 'failed'
		self.iface= None
		self.ip= None
		self.opcode = ''
		self.gateway = None
		self.nameservers = list()
		self.tx_bytes=0
		self.rx_bytes=0
		self.connected_time=0
		self.simple_status= None
		self.reenable = False
		self.removed = 0
		self.initessentials(obj)
	def initessentials(self,obj):

		self.modem = obj.get_modem()
		self.modem_3gpp = obj.get_modem3gpp()
		self.modem_simple = obj.get_modem_simple()
		self.modem.connect('state_changed',self.state_handler)
		self.index = self.modem_index(self.modem.dup_device())
		path=str(obj.get_object_path())
		self.mindex = int(path[path.rfind("/")+1:])
		self.cprint('imei', '')
		self.status = ModemManager.modem_state_get_string(self.modem.get_state())
		if self.modem.get_state() == ModemManager.ModemState.FAILED:
			print('[ModemWatcher,%s] ignoring failed modem' %
				  str(self.index))
		elif self.modem.get_state() == ModemManager.ModemState.DISABLED:
			res = self.modem.enable()

		elif self.modem.get_state() == ModemManager.ModemState.REGISTERED:
			self.connect_modem()
		elif self.modem.get_state() == ModemManager.ModemState.CONNECTED:
			code = self.modem_3gpp.get_operator_code()
			if code is not None:
				self.simple_status = self.modem_simple.get_status_sync(Gio.Cancellable.new())
				stype = self.simple_status.get_access_technologies()
				squal, rc = self.simple_status.get_signal_quality()

				self.cprint('csq', str(squal))
				if int(stype)!=0:
					if int(stype) >16:
						self.cprint('mode', 'HSDPA')
					else:
						self.cprint('mode', 'GSM')
				GLib.timeout_add_seconds(5, self.signal_notifier)
				b = self.modem.list_bearers_sync(Gio.Cancellable.new())
				self.bearer = b[0]
				self.connect_iface()
			else:
				self.enable()
	def modem_index(self,path):
		mdems1 = ['1-1','1-2','1-4.1.1', '1-4.1.2', '1-4.1.3', '1-4.1.4', '1-4.2', '1-4.3', '1-4.4']
		mdems2 = ['2-1','2-2','2-4.1.1', '2-4.1.2', '2-4.1.3', '2-4.1.4', '2-4.2', '2-4.3', '2-4.4']
		path=str(path)
		path= path[path.rfind("/")+1:]
		if path.startswith('1'):
			mdems=mdems1
		else:
			mdems=mdems2

		if path in mdems:
			mindex = mdems.index(str(path))
		else:
			mindex=random.randint(8, 30)
		return mindex

	def enable(self):
		cmd = ["mmcli", "-m", str(self.mindex), "--set-allowed-modes='3G|2G'", "--set-preferred-mode='UMTS'"]
		print " ".join(cmd)
		#subprocess.call(cmd)
		self.modem.enable()
	def disable(self,reenable):
		if reenable:
			self.reenable = True
			self.cprint('connection', 'disconnected')
		else:
			self.cprint('connection', 'No Internet')
		self.modem.disable()

	def connect_modem(self):
		code = self.modem_3gpp.get_operator_code()
		if code is not None:

			self.simple_status = self.modem_simple.get_status_sync(Gio.Cancellable.new())
			stype = self.simple_status.get_access_technologies()
			squal, rc = self.simple_status.get_signal_quality()

			self.cprint('csq', str(squal))
			if int(stype) != 0:
				if int(stype) > 16:
					self.cprint('mode', 'HSDPA')
				else:
					self.cprint('mode', 'GSM')

			self.brp = ModemManager.BearerProperties.new()
			self.bearer = None
			apn = ''
			self.cprint('operator', code)
			self.opcode = code
			# modem_3gpp.unref()
			if code == '47001':
				apn = 'gpinternet'
			if code == '47002':
				apn = 'internet'
			if code == '47007':
				apn = 'internet'
			elif code == '47003':
				apn = 'blweb'
			elif code == '47004':
				apn = 'teletalk.internet'
			self.brp.set_apn(apn)
			b = self.modem.list_bearers_sync(Gio.Cancellable.new())
			if len(b) == 0:
				self.bearer = self.modem.create_bearer_sync(self.brp, Gio.Cancellable.new())
			else:
				self.bearer = b[0]
			self.bearer.connect()
			GLib.timeout_add_seconds(5, self.signal_notifier)
		else:
			self.enable()

	def connect_iface(self):
		if self.bearer is not None and self.bearer.get_connected() == True and self.bearer.get_ipv4_config().get_address() is not None:
			ifaceipremover(self.bearer.get_interface())
			ip4 = self.bearer.get_ipv4_config()

			self.iface = self.bearer.get_interface()
			self.ip = self.bearer.get_ipv4_config()
			self.gateway = ip4.get_gateway()
			self.nameservers = ip4.get_dns()
			ipn = IPNetwork(str(ip4.get_address())+"/"+str(ip4.get_prefix()))
			intn=ipn.network
			os.system("sudo ip link set " + self.bearer.get_interface() + " up")
			os.system("sudo ip addr add "+str(ip4.get_address())+"/"+str(ip4.get_prefix())+" broadcast "+str(ipn.broadcast)+" dev "+self.bearer.get_interface())
			os.system("sudo ip rule add from " + str(ip4.get_address()) + " table " + self.bearer.get_interface())
			os.system(
				"sudo ip route add " + str(ipn.network) + "/" + str(
					ip4.get_prefix()) + " dev " + self.bearer.get_interface() + " scope link table " + str(
					self.bearer.get_interface()))
			os.system(
				"sudo ip route add default via " + ip4.get_gateway() + " dev " + self.bearer.get_interface() + " table " + str(
					self.bearer.get_interface()))
			os.system("sudo ip link set mtu 9000 dev " + self.bearer.get_interface())
			os.system("sudo ip link set dev " + self.bearer.get_interface() + " multipath on")
			words = file_content("/etc/iproute2/rt_tables").split()
			if (self.bearer.get_interface() not in words):
				nl = int(words[len(words) - 2]) + 1
				cnt = str(nl) + "  " + self.bearer.get_interface() + "\r\n"
				append_content("/etc/iproute2/rt_tables", cnt)

			nms=''
			if self.checked_internet==True:
				self.checked_internet == True
				for line in file_content("/etc/resolv.conf").split('\n'):
					if line.startswith("nameserver"):
						nameservers.append(line[line.find(" ") + 1:])
				nameserver=ip4.get_dns()[0]
				append_resolv(nameserver)
				os.system("sudo route add default gw " + ip4.get_gateway() + " " + self.bearer.get_interface())
				cmd = ["ping", "-I", self.bearer.get_interface(), "-c", "1", "-i", "3", "-w", "4", "8.8.8.8"]
				print " ".join(cmd)
				retcode = subprocess.call(cmd)
				logging.debug("RETCODE============" + str(retcode) + "========================")
				if retcode != 0:
					cmd = ["ping", "-I", self.bearer.get_interface(), "-c", "1", "-i", "3", "-w", "4", "8.8.4.4"]
					print " ".join(cmd)
					retcode = subprocess.call(cmd)
					# time.sleep(5)
					# time.sleep(5)
					logging.debug("RETCODE============" + str(retcode) + "========================")
					if retcode != 0:
						os.system("sudo ip route del default via " +  ip4.get_gateway())
						logging.debug("Taking down interface: " +  ip4.get_gateway())
						self.disconnect()
						self.disable(False)


			self.cprint('connection', 'connected')
			self.status = 'connected'
			self.connected_time = time.time()

		else:
			GLib.timeout_add_seconds(1, self.connect_iface)


	def signal_notifier(self):
		if self.status!='disconnected':
			if self.status =='disabled':
				if self.reenable == True:
					self.modem.enable()
					GLib.timeout_add_seconds(2, self.signal_notifier)
			elif self.modem_3gpp is not None:
				code = self.modem_3gpp.get_operator_code()
				if code!='' and code is not None:
					self.cprint('operator', code)
				try:
					self.simple_status = self.modem_simple.get_status_sync(Gio.Cancellable.new())
					stype = self.simple_status.get_access_technologies()
					squal, rc = self.simple_status.get_signal_quality()
					self.cprint('csq', str(squal))
					if int(stype)!=0:
						if int(stype) >16:
							self.cprint('mode', 'HSDPA')
							GLib.timeout_add_seconds(10, self.signal_notifier)
						else:
							self.cprint('mode', 'GSM')
							GLib.timeout_add_seconds(2, self.signal_notifier)
				except:
					logging.debug("Modem: "+str(self.index)+"  Status: "+self.status+"  "+str(self.mindex) )

	def disconnect(self):
		if self.iface is not None:
			ifaceipremover(self.iface)
		self.cprint('connection', 'disconnected')
		self.status = 'disconnected'
		if self.reenable == True:
			self.modem.enable()
		if self.bearer is not None:
			self.bearer.disconnect()
	def remove(self):
		if self.iface is not None:
			ifaceipremover(self.iface)
		self.cprint('connection', 'removed')
		os.system("mmcli -S")
		self.status = 'disconnected'
		if self.bearer is not None:
			self.bearer.disconnect()
		#self.modem = None
		#self.modem_3gpp = None
		#self.modem_simple = None
		self.removed+=1
		print "modem: "+str(self.index)+" removed: "+str(self.removed)


	def state_handler(self,modem,old,new,reason):
		self.status = ModemManager.modem_state_get_string(self.modem.get_state())
		logging.debug("Modem: "+str(self.index)+"  oldstat: "+str(old)+"  newstatus: "+str(new))
		if old < 8 and new == 8:
			try:
				self.connect_modem()
			except Exception:
				logging.debug("Could not connect this modem")
		elif new==11:
			self.connect_iface()
		#elif old > 11 and new < 11 and self.status != 'disconnected' :
		 #   self.disconnect()






	def cprint(self, action, result):
		# sys.stdout.flush()
		self.q.put_nowait("MODEM:" + str(self.index) + ":" + action + ":" + result)


