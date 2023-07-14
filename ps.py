#!/usr/bin/python3

#exec(open('ps.py').read())
import socket
import struct
import time
import zlib
import datetime
import json
import numpy
import uuid

addr = ('172.17.2.14', 59613)
clients = []
clients.append('192.168.1.17')
clients.append('192.168.1.18')
#clients.append('172.17.2.14')
#clients.append('172.17.2.17')
#clients.append('172.17.2.2')
#clients.append('172.17.3.111')

device_uuid = str(uuid.uuid4())
match_uuid = str(uuid.uuid4())

class Device:
	port = 59613
	
	def __init__(self, addr):
		self.addr = addr
		self.match_def = {}
		self.match_scores = {}
		self.status = {}
		self.modified_date = '1970-01-01 00:00:00.000'
		
	def poll_status(self):
		sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		sock.settimeout(5)
		try:
			sock.connect((self.addr, self.port))
			request = dict([
				('ps_name', socket.gethostname()),
				('ps_port', self.port),
				('ps_host', sock.getsockname()[0]),
				('ps_matchname', socket.gethostname()),
				('ps_matchid', match_uuid),
				('ps_modified', time.strftime('%Y-%m-%d %H:%M:%S.000')),
				('ps_battery', 100),
				('ps_uniqueid', device_uuid)
			])
			json_request = json.dumps(request)
			header = struct.pack('!IIIII',0x19113006, len(json_request), 6, 4, int(time.time()))
			sock.sendall(header + json_request.encode())
			time.sleep(1)
			response_header = dict(zip(['signature','length','type','flags','time'],struct.unpack('!IIIII',sock.recv(20))))
			response = sock.recv(response_header['length'])
			self.status = json.loads(response)
		finally:
			sock.close()
			return
		
	def poll_match(self):
		sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		sock.settimeout(5)
		try:
			sock.connect((self.addr, self.port))
			header = struct.pack('!IIIII',0x19113006, 0, 8, 4, int(time.time()))
			sock.sendall(header)
			time.sleep(1)
			response_header = dict(zip(['signature','length','type','flags','time'],struct.unpack('!IIIII',sock.recv(20))))
			response = sock.recv(response_header['length'])
			match_def_length = struct.unpack('!I',response[0:4])[0]
			self.match_def = json.loads(zlib.decompress(response[4:match_def_length+4]))
			self.match_scores = json.loads(zlib.decompress(response[match_def_length+4:]))
			self.modified = match_def.match_modifieddate
		finally:
			sock.close()
			return
	
	def poll(self):
		self.poll_status()
		if 'ps_modified' in self.status:
			modified_date = self.status['ps_modified']
			if modified_date != self.modified_date:
				modified_time_1 = datetime.datetime.strptime(modified_date,'%Y-%m-%d %H:%M:%S.%f')
				modified_time_2 = datetime.datetime.strptime(self.modified_date,'%Y-%m-%d %H:%M:%S.%f')
				if modified_time_1 > modified_time_2:
					self.poll_match()
	
	def __str__(self):
		if 'ps_name' in self.status:
			return '{}'.format(self.status['ps_name'])
		else:
			return 'Unknown'
			
	def __repr__(self):
		if 'ps_name' in self.status:
			return '<{} "{}">'.format(self.__class__.__name__, self.status['ps_name'])
		else:
			return '<{} "{}">'.format(self.__class__.__name__, 'Unknown')

class Match:
	def __init__(self, match_def, match_scores):
		self.id = match_def['match_id']
		self.shooters = {}
		self.stages = {}
		self.scores = {}
		self.data = {}
		self.update_match_data(match_def)
		if 'match_shooters' in match_def:
			self.update_shooters(match_def['match_shooters'])
		if 'match_stages' in match_def:
			self.update_stages(match_def['match_stages'])
		if 'match_scores' in match_scores:
			self.update_scores(match_scores['match_scores'])
	
	def update(self, match_def, match_scores):
		modified_date = match_def['match_modifieddate']
		if modified_date != self.modified_date:
			modified_time_1 = datetime.datetime.strptime(modified_date,'%Y-%m-%d %H:%M:%S.%f')
			modified_time_2 = datetime.datetime.strptime(self.modified_date,'%Y-%m-%d %H:%M:%S.%f')
			if modified_time_1 > modified_time_2:
				self.update_match_data(match_def)
		if 'match_shooters' in match_def:
			self.update_shooters(match_def['match_shooters'])
		if 'match_stages' in match_def:
			self.update_stages(match_def['match_stages'])
		if 'match_scores' in match_scores:
			self.update_scores(match_scores['match_scores'])
	
	def update_match_data(self, match_def):
		self.name = match_def['match_name']
		self.penalties = match_def['match_penalties']
		self.penalties_value = numpy.array([penalty['pen_val'] for penalty in self.penalties])
		self.divisions = match_def['match_cats']
		self.modified_date = match_def['match_modifieddate']
		self.type = match_def['match_type']
		self.subtype = match_def['match_subtype']
	
	def update_scores(self, scores):
		for stage in scores:
			for shooter_score in stage['stage_stagescores']:
				shooter_id = shooter_score['shtr']
				stage_id = stage['stage_uuid']
				if shooter_id in self.shooters:
					if stage_id in self.shooters[shooter_id].scores:
						self.shooters[shooter_id].scores[stage_id].update_if_modified(shooter_score)
					else:
						self.shooters[shooter_id].scores[stage_id] = StageScore(stage_id, shooter_score, self.penalties_value)
	
	def update_stage(self, stage):
		if stage['stage_uuid'] in self.stages:
			self.stages[stage['stage_uuid']].update_if_modified(stage)
		else:
			self.stages[stage['stage_uuid']] = Stage(stage)
	
	def update_stages(self, stages):
		for stage in stages:
			self.update_stage(stage)
	
	def update_shooter(self, shooter):
		if shooter['sh_uid'] in self.shooters:
			self.shooters[shooter['sh_uid']].update_if_modified(shooter)
		else:
			self.shooters[shooter['sh_uid']] = Shooter(shooter)
	
	def update_shooters(self, shooters):
		for shooter in shooters:
			self.update_shooter(shooter)
			
	def print_stage(self, stage):
		print(self.stages[stage].name)
		for score in self.scores[stage]:
			print(self.shooters[score],self.scores[stage][score].total)
	
	def print_stages(self):
		for stage in self.stages:
			self.print_stage(stage)
	
	def print_stage_names(self):
		print(', '.join('{}: {}'.format(self.stages[stage].number, self.stages[stage].short_name()) for stage in self.stages))
	
	def html_head(self):
		return f'<!DOCTYPE html><html lang="en"><head><title>{self.name}</title><meta http-equiv="refresh" content="5;url=/home/kiosk/index.html" /><style>html {{font-family: "Helvetica" }} tr:nth-child(even) {{background: #CCC}} td{{text-align: right; padding: 2px 10px}} th{{padding: 2px 10px}} </style></head><body>'
	
	def html_foot(self):
		html = 'Divisions: ' + ', '.join((division for division in self.divisions))
		html += '<br />Last Updated: #</body></html>'
		return html
		
	def html_title(self):
		html = f'{self.name} (Last Updated: {self.modified_date})<br />'
		#html += ', '.join(('Stage {}: {}'.format(self.stages[stage].number,self.stages[stage].short_name())) for stage in self.stages)
		html += '<table>'
		return html
	
	def html_header(self):
		return '<tr><th>'+'</th><th>'.join(['Place', 'Name', 'Division', 'Time', ''])+'</th><th>'.join(('Stage {}<br /><span style="font-size: x-small">{}</span>'.format(self.stages[stage].number, self.stages[stage].short_name()) for stage in self.stages))+'</th></tr>'
	
	def html_table(self):
		html = ''
		for index, item in enumerate(sorted(self.data, key=lambda x: x['total'])):
			html += '<tr><td style="text-align: center">'
			html += '</td><td style="text-align: center">'.join(['{}'.format(index+1), item['name'], item['division'], item['total_string']]) + '</td><td>'
			html += '</td><td>'.join((item['score_string'][stage] for stage in self.stages))
			html += '</td></tr>'
		return html + '</table>'
	
	def html(self):
		return self.html_head()+self.html_title()+self.html_header()+self.html_table()+self.html_foot()
	
	def generate_html(self):
		self.generate_table()
		with open('index.html', 'w') as f:
			f.write(self.html())
	
	def print_title(self):
		print(self.name)
		print(self.modified_date)
	
	def print_header(self):
		print(', '.join(['Place, Name, Division, Time',', '.join(('Stage {}'.format(self.stages[stage].number) for stage in self.stages))]))
	
	def print_table(self):
		for index, item in enumerate(sorted(self.data, key=lambda x: x['total'])):
			print(', '.join(['{}'.format(index+1), item['name'], item['division'], item['total_string'], ', '.join((item['score_string'][stage] for stage in self.stages))]))
	
	def generate_table(self):
		self.data = []
		
		stages = [self.stages[stage] for stage in self.stages]
		
		for shooter in self.shooters:
			item = {}
			item['name'] = self.shooters[shooter].name()
			item['division'] = self.shooters[shooter].short_division()
			item['score'] = {}
			item['score_string'] = {}
			for stage in stages:
				item['score'][stage.id] = self.shooters[shooter].score(stage)
				item['score_string'][stage.id] = self.shooters[shooter].score_string(stage)
			item['total'] = self.shooters[shooter].total(stages)
			item['total_string'] = self.shooters[shooter].total_string(stages)
			self.data.append(item)
	
	def print_header2(self):
		print(self.name)
		print(self.modified_date)
		self.print_stage_names()
		print('Name', end='')
		for stage in self.stages:
			print(',{}'.format(self.stages[stage].name), end='')
		print()
		for shooter in self.shooters:
			print('"{}"'.format(self.shooters[shooter]), end='')
			for stage in self.stages:
				if stage in self.scores:
					if shooter in self.scores[stage]:
						if self.scores[stage][shooter].dnf:
							print(',DNF', end='')
						else:
							print(',{:.2f}'.format(self.scores[stage][shooter].total), end='')
					else:
						print(',', end='')
				else:
					print(',', end='')
			print()
	
	def __str__(self):
		return self.name
			
	def __repr__(self):
		return '<{} "{}", "{}", "{}">'.format(self.__class__.__name__, self.name, self.type, self.subtype)

class Stage:
	def __init__(self, stage):
		self.id = stage['stage_uuid']
		self.update(stage)
	
	def update_if_modified(self, stage):
		modified_date = stage['stage_modifieddate']
		if modified_date != self.modified_date:
			modified_time_1 = datetime.datetime.strptime(modified_date,'%Y-%m-%d %H:%M:%S.%f')
			modified_time_2 = datetime.datetime.strptime(self.modified_date,'%Y-%m-%d %H:%M:%S.%f')
			if modified_time_1 > modified_time_2:
				self.update(stage)
	
	def update(self, stage):
		self.name = stage['stage_name']
		self.number = stage['stage_number']
		self.remove_worst_string = stage['stage_removeworststring']
		self.modified_date = stage['stage_modifieddate']
		self.strings = stage['stage_strings']
		self.max_time = 30 * (self.strings - self.remove_worst_string)
	
	def short_name(self):
		if self.name == 'W1 ARG: Accelerator':
			return 'Accelerator'
		elif self.name == 'W1 CB: The Pendulum':
			return 'The Pendulum'
		elif self.name == 'W1 ARB: Five To Go':
			return 'Five To Go'
		elif self.name == 'W1 CG: Roundabout':
			return 'Roundabout'
		elif self.name == 'W3 ALB: Speed Option':
			return 'Speed Option'
		elif self.name == 'W3 BG: Showdown':
			return 'Showdown'
		elif self.name == 'W3 ALG: Outer Limits':
			return 'Outer Limits'
		elif self.name == 'W3 BB: Smoke & Hope':
			return 'Smoke & Hope'
		else:
			return self.name
	
	def __str__(self):
		return self.name
	
	def __repr__(self):
		return '<{} "{}">'.format(self.__class__.__name__, self.name)

class StageScore:
	def __init__(self, stage, score, penalties_value):
		self.stage_id = stage
		self.shooter_id = score['shtr']
		self.penalties_value = penalties_value
		self.update(score)
	
	def update_if_modified(self, score):
		modified_date = score['mod']
		if score['mod'] != self.modified_date:
			modified_time_1 = datetime.datetime.strptime(modified_date,'%Y-%m-%d %H:%M:%S.%f')
			modified_time_2 = datetime.datetime.strptime(self.modified_date,'%Y-%m-%d %H:%M:%S.%f')
			if modified_time_1 > modified_time_2:
				self.update(score)
	
	def update(self, score):
		if 'aprv' in score:
			self.approved = score['aprv']
		else:
			self.approved = False
		if 'dnf' in score:
			self.dnf = score['dnf']
		else:
			self.dnf = False
		self.strings = numpy.array(score['str'])
		if 'penss' in score:
			self.penalty_array = numpy.array(score['penss'])
			self.penalties = [sum(penalties) for penalties in self.penalty_array*self.penalties_value]
			self.strings_with_penalties = self.strings + self.penalties
		else:
			self.strings_with_penalties = self.strings
		limit = self.strings_with_penalties.clip(None, 30)
		worst = max(limit)
		self.total = sum(limit)-worst
		self.modified_date = score['mod']
		
	def __str__(self):
		return '{:.2f}'.format(self.total)
	
	def __repr__(self):
		return '<{} {:.2f}>'.format(self.__class__.__name__, self.total)

#	def with_penalties(self, penalties_value):
#		score_penalties = [sum(penalties) for penalties in self.penalties*self.penalties_value]
#		return self.strings+score_penalties

#			if penalty.
#			for index, penalty in enumerate(string):
#				if 

class Shooter:
	def __init__(self, shooter):
		self.id = shooter['sh_uid']
		self.update(shooter)
		self.scores = {}
	
	def update_if_modified(self, shooter):
		modified_date = shooter['sh_mod']
		if shooter['sh_mod'] != self.modified_date:
			modified_time_1 = datetime.datetime.strptime(modified_date,'%Y-%m-%d %H:%M:%S.%f')
			modified_time_2 = datetime.datetime.strptime(self.modified_date,'%Y-%m-%d %H:%M:%S.%f')
			if modified_time_1 > modified_time_2:
				self.update(shooter)
	
	def update(self, shooter):
		self.firstname = shooter['sh_fn']
		self.lastname = shooter['sh_ln']
		self.division = shooter['sh_dvp']
		self.deleted = shooter['sh_del']
		self.modified_date = shooter['sh_mod']
		self.disqualified = shooter['sh_dq']
		self.deleted = shooter['sh_del']
	
	def short_division(self):
		if self.division == 'Rimfire':
			return 'R'
		elif self.division == 'Rimfire Revolver':
			return 'RR'
		elif self.division == 'Rimfire Optic':
			return 'RO'
		elif self.division == 'Rimfire Revolver Optic':
			return 'RRO'
		elif self.division == 'Centrefire':
			return 'C'
		elif self.division == 'Centrefire Revolver':
			return 'CR'
		elif self.division == 'Centrefire Optic':
			return 'CO'
		elif self.division == 'Centrefire Revolver Optic':
			return 'CRO'
		else:
			return self.division
	
	def score(self, stage):
		if not self.disqualified and stage.id in self.scores:
			score = self.scores[stage.id]
			if score.approved and not score.dnf:
				return score.total
		return stage.max_time
	
	def score_string(self, stage):
		if stage.id in self.scores:
			score = self.scores[stage.id]
			if score.dnf:
				return 'DNF'
			if score.approved:
				return '{:.2f}'.format(score.total)
		return '-'
	
	def total(self, stages):
		return sum((self.score(stage) for stage in stages))
	
	def total_string(self, stages):
		if self.disqualified:
			return 'DQ'
		return '{:.2f}'.format(self.total(stages))
	
	#def set_score(self, stage, score)
	
	def name(self):
		return '{} {}'.format(self.firstname, self.lastname)
	
	def __repr__(self):
		return '<{} "{}", "{}", "{}">'.format(self.__class__.__name__, self.firstname, self.lastname, self.division)

devices = {}
for client in clients:
	devices[client] = Device(client)
	devices[client].poll()
	#print(devices[client])

while True:
	for client in clients:
		devices[client].poll()
		matches = {}
	for device in devices:
		if 'match_id' in devices[device].match_def:
			id = devices[device].match_def['match_id']
			if id in matches:
				matches[id].update(devices[device].match_def, devices[device].match_scores)
			else:
				matches[id] = Match(devices[device].match_def, devices[device].match_scores)
	if len(matches) > 0:
		m1 = matches[next(iter(matches))]
		m1.generate_html()
	time.sleep(10)

#s1 = m1.shooters[next(iter(m1.shooters))]


#def scan(clients):
#	for client in clients:
#		sock = scan_connect(client)
#		scan_request_status(sock)
#		time.sleep(1)
#		header = scan_receive_header(sock)
#		status = ps_readdata(sock, header[1])
#		sta = json.loads(status)
#		scan_print_status(sta)

#def scan_connect(ip):
#	s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
#	s.settimeout(1)
#	s.connect((ip, 59613))
#	return s

#def scan_request_status(sock):
#	status = dict([
#		('ps_name', socket.gethostname()),
#		('ps_port', 59613),
#		('ps_host', sock.getsockname()[0]),
#		('ps_matchname', socket.gethostname()),
#		('ps_matchid', match_uuid),
#		('ps_modified', time.strftime('%Y-%m-%d %H:%M:%S.000')),
#		('ps_battery', 100),
#		('ps_uniqueid', device_uuid)
#		])
#	json_status = json.dumps(status)
#	message = struct.pack('!IIIII',0x19113006, len(json_status), 6, 4, int(time.time()))
#	sock.sendall(message + json_status.encode())

#def scan_receive_header(sock):
#	header = struct.unpack('!IIIII',sock.recv(20))
#	return header

#def scan_print_status(status):
#	print('{}: {}'.format(status['ps_name'], status['ps_matchname']))

#def ps_connect(ip):
#	s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
#	s.settimeout(1)
#	s.connect(ip)
#	return s
#
#def ps_requestmatch(sock):
#	sock.sendall(struct.pack('!IIIII',0x19113006, 0, 8, 4, int(time.time())))

#def ps_readheader(sock):
#	header = struct.unpack('!IIIII',sock.recv(20))
#	return header

#def ps_printheader(header):
#	print('Header',hex(header[0]))
#	print('Length',header[1])
#	print('Type',header[2])
#	print('Flags',header[3])
#	print('UnixTime',datetime.datetime.fromtimestamp(header[4]))

#def ps_readdata(sock, length):
#	data = sock.recv(length)
#	return data

#def ps_split(data):
#	split = struct.unpack('!I',data[0:4])[0]
#	match_def = zlib.decompress(data[4:split+4])
#	match_scores = zlib.decompress(data[split+4:])
#	return (match_def, match_scores)
