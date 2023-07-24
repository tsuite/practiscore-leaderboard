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
import configparser
import os
import select
import errno
import textwrap

#config = configparser.RawConfigParser()
#config.read('ps.ini')

#addr = ('172.17.2.14', 59613)
#clients.append('192.168.1.17')
#clients.append('192.168.1.18')
#clients.append('172.17.2.14')
#clients.append('172.17.2.17')
#clients.append('172.17.2.2')
#clients.append('172.17.3.111')

#device_uuid = str(uuid.uuid4())
#match_uuid = str(uuid.uuid4())

class Kiosk:
	style = '<style>.left{text-align: left} .right{text-align: right} .center{text-align: center} html {font-family: "Helvetica"; font-size: large } tr:nth-child(even) {background: #DDD} td{padding: 2px 10px} th{padding: 2px 10px} .device-status{color: #CCC} </style>'
	
	def __init__(self):
		self.device_config = configparser.RawConfigParser()
		self.device_config.read('ps-devices.ini')
		device_uuid = str(uuid.uuid4())
		match_uuid = str(uuid.uuid4())
		self.devices = {}
		index = 0
		for poll_offset, name in enumerate(self.device_config.sections()):
			self.devices[name] = Device(self.device_config[name], device_uuid, match_uuid, poll_offset)
			self.devices[name].poll()

	def loop(self):
		while True:
			self.matches = {}
			for device in self.devices:
				self.devices[device].poll()
				if 'match_id' in self.devices[device].match_def:
					id = self.devices[device].match_def['match_id']
					if id in self.matches:
						self.matches[id].update(self.devices[device].match_def, self.devices[device].match_scores)
					elif self.devices[device].match_def['match_subtype'] == 'scsa':
						self.matches[id] = SCSAMatch(self.devices[device].match_def, self.devices[device].match_scores)
					elif self.devices[device].match_def['match_subtype'] == 'nra':
						self.matches[id] = NRAMatch(self.devices[device].match_def, self.devices[device].match_scores)
					elif self.devices[device].match_def['match_subtype'] == 'ipsc':
						self.matches[id] = IPSCMatch(self.devices[device].match_def, self.devices[device].match_scores)
					else:
						self.matches[id] = Match(self.devices[device].match_def, self.devices[device].match_scores)
			if len(self.matches) > 0:
				m1 = self.matches[next(iter(self.matches))]
			self.generate_html()
			time.sleep(1)
	
	def generate_html(self):
		html = f'<!DOCTYPE html><html lang="en"><head>{self.generate_html_head()}</head><body>{self.generate_html_body()}</body></html>'
		with open('/mnt/ramdisk/match.html', 'w') as f:
			f.write(html)
		
	def now(self):
		return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
	
	def generate_html_head(self):
		title = f'<title>{self.now()}</title>'
		return f'{title}{self.style}'
	
	def generate_html_body(self):
		body = []
		body.append(f'{self.now()}<br>')
		for match in self.matches:
			body.append(f'{self.matches[match].html()}<br>')
		for device in self.devices:
			body.append(f'{self.devices[device].html()}<br>')
		return ''.join(body)

class Device:
	def __init__(self, device_data, device_uuid, match_uuid, poll_offset):
		self.address = device_data.get('Address')
		self.port = device_data.getint('Port', 59613)
		self.name = device_data.name
		self.timeout = device_data.getint('Timeout', 1)
		self.poll_time = device_data.getint('PollTime', 10)
		self.poll_counter = poll_offset
		self.slow_poll = device_data.getint('SlowPoll', 5)
		self.slow_poll_counter = 0
		self.shutdown = device_data.get('Shutdown','')
		self.match_def = {}
		self.match_scores = {}
		self.status = {}
		self.update_date = 'Unknown'
		self.online = False
		self.match_uuid = match_uuid
		self.device_uuid = device_uuid
	
	def html(self):
		if 'match_name' in self.match_def:
			return f'<span class="device-status">{self.name}: {self.match_def["match_name"]}, {self.update_date} {"Online" if self.online else "Offline"} ({self.poll_counter + self.poll_time*self.slow_poll_counter})</span>'
		else:
			return f'<span class="device-status">{self.name}: {self.update_date} {"Online" if self.online else "Offline"} ({self.poll_counter + self.poll_time*self.slow_poll_counter})</span>'
	
	def poll_status(self):
		sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		sock.settimeout(self.timeout)
		try:
			sock.connect((self.address, self.port))
			request = dict([
				('ps_name', socket.gethostname()),
				('ps_port', self.port),
				('ps_host', sock.getsockname()[0]),
				('ps_matchname', socket.gethostname()),
				('ps_matchid', self.match_uuid),
				('ps_modified', time.strftime('%Y-%m-%d %H:%M:%S.000')),
				('ps_battery', 100),
				('ps_uniqueid', self.device_uuid)
			])
			json_request = json.dumps(request)
			header = struct.pack('!IIIII',0x19113006, len(json_request), 6, 4, int(time.time()))
			sock.sendall(header + json_request.encode())
			time.sleep(1)
			response_header = dict(zip(['signature','length','type','flags','time'],struct.unpack('!IIIII',sock.recv(20))))
			response = sock.recv(response_header['length'])
			self.status = json.loads(response)
		except Exception:
			self.slow_poll_counter = max(self.slow_poll-1,0)
		finally:
			sock.close()
		
	def poll_match(self):
		sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		sock.settimeout(self.timeout)
		try:
			sock.connect((self.address, self.port))
			header = struct.pack('!IIIII',0x19113006, 0, 8, 4, int(time.time()))
			sock.sendall(header)
			time.sleep(1)
			response_header = dict(zip(['signature','length','type','flags','time'],struct.unpack('!IIIII',sock.recv(20))))
			response = sock.recv(response_header['length'])
			match_def_length = struct.unpack('!I',response[0:4])[0]
			self.match_def = json.loads(zlib.decompress(response[4:match_def_length+4]))
			with open(f'/mnt/ramdisk/match_def_{self.name}.json', 'w') as f:
				f.write(json.dumps(self.match_def))
			if len(response) > match_def_length + 4:
				self.match_scores = json.loads(zlib.decompress(response[match_def_length+4:]))
				with open(f'/mnt/ramdisk/match_scores_{self.name}.json', 'w') as f:
					f.write(json.dumps(self.match_scores))
		except Exception:
			self.slow_poll_counter = max(self.slow_poll-1,0)
			self.online = False;
		else:
			self.update_date = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
			self.online = True;
		finally:
			sock.close()
	
	def poll(self):
		if self.poll_counter < 1 and self.slow_poll_counter < 1:
			self.poll_counter = self.poll_time
			self.poll_status()
			if self.shutdown != '' and 'ps_matchid' in self.status:
				if self.status['ps_matchid'] == self.shutdown:
					os.system('/usr/bin/sudo /usr/sbin/shutdown -h now')
			#if 'ps_modified' in self.status:
				#modified_date = self.status['ps_modified']
				#print (f'{self.modified_date} {modified_date}')
				#if modified_date != self.modified_date:
					#modified_time_1 = datetime.datetime.strptime(modified_date,'%Y-%m-%d %H:%M:%S.%f')
					#modified_time_2 = datetime.datetime.strptime(self.modified_date,'%Y-%m-%d %H:%M:%S.%f')
					#if modified_time_1 > modified_time_2:
						#self.poll_match()
			self.poll_match()
		elif self.poll_counter < 1:
			self.slow_poll_counter -= 1
			self.poll_counter = self.poll_time
		else:
			self.poll_counter -= 1
	
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
		self.update(match_def, match_scores)
	
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
	
	def html(self):
		return f'Match Type {self.subtype} not supported'
		
	def update_match_data(self, match_def):
		self.name = match_def['match_name']
		self.divisions = match_def['match_cats']
		self.modified_date = match_def['match_modifieddate']
		self.type = match_def['match_type']
		self.subtype = match_def['match_subtype']
	
	def update_scores(self, match_scores):
		for stage in match_scores:
			for stage_stagescore in stage['stage_stagescores']:
				shooter_id = stage_stagescore['shtr']
				stage_id = stage['stage_uuid']
				if shooter_id in self.shooters:
					if stage_id in self.shooters[shooter_id].scores:
						self.shooters[shooter_id].scores[stage_id].update_if_modified(stage_stagescore)
					elif self.subtype == 'scsa':
							self.shooters[shooter_id].scores[stage_id] = SCSAStageScore(self, stage_id, stage_stagescore)
					elif self.subtype == 'nra':
							self.shooters[shooter_id].scores[stage_id] = NRAStageScore(self, stage_id, stage_stagescore)
					elif self.subtype == 'ipsc':
							self.shooters[shooter_id].scores[stage_id] = IPSCStageScore(self, stage_id, stage_stagescore)
	
	def update_stage(self, match_stage):
		if match_stage['stage_uuid'] in self.stages:
			self.stages[match_stage['stage_uuid']].update_if_modified(match_stage)
		elif self.subtype == 'scsa':
			self.stages[match_stage['stage_uuid']] = SCSAStage(self, match_stage)
		elif self.subtype == 'nra':
			self.stages[match_stage['stage_uuid']] = NRAStage(self, match_stage)
		elif self.subtype == 'ipsc':
			self.stages[match_stage['stage_uuid']] = IPSCStage(self, match_stage)
		else:
			self.stages[match_stage['stage_uuid']] = Stage(self, match_stage)
		
	
	def update_stages(self, match_stages):
		for match_stage in match_stages:
			self.update_stage(match_stage)
	
	def update_shooter(self, match_shooter):
		if match_shooter['sh_uid'] in self.shooters:
			self.shooters[match_shooter['sh_uid']].update_if_modified(match_shooter)
		elif self.subtype == 'scsa':
			self.shooters[match_shooter['sh_uid']] = SCSAShooter(match_shooter)
		elif self.subtype == 'nra':
			self.shooters[match_shooter['sh_uid']] = NRAShooter(match_shooter)
		elif self.subtype == 'ipsc':
			self.shooters[match_shooter['sh_uid']] = IPSCShooter(match_shooter)
		else:
			self.shooters[match_shooter['sh_uid']] = Shooter(match_shooter)
	
	def update_shooters(self, match_shooters):
		for match_shooter in match_shooters:
			self.update_shooter(match_shooter)
	
	def __str__(self):
		return self.name
			
	def __repr__(self):
		return '<{} "{}", "{}", "{}">'.format(self.__class__.__name__, self.name, self.type, self.subtype)

class SCSAMatch(Match):
	def update_match_data(self, match_def):
		super().update_match_data(match_def)
		self.penalties = match_def['match_penalties']
		self.penalties_value = numpy.array([penalty['pen_val'] for penalty in self.penalties])
	
	def html_header(self):
		return '<tr><th class="center">'+'</th><th class="center">'.join(['#', 'Name', 'Div.', 'Time', ''])+'</th><th class="center">'.join(('Stage {}<br><span style="font-size: x-small">{}</span>'.format(self.stages[stage].number, self.stages[stage].short_name) for stage in self.stages))+'</th></tr>'
	
	def html_table(self):
		rows = []
		for index, item in enumerate(sorted(self.data, key=lambda x: x['total'])):
			row = []
			row.append(f'<td class="center">{index+1}</td><td>{item["name"]}</td><td class="center">{item["division"]}</td><td class="right">{item["total_string"]}</td>')
			for stage in self.stages:
				row.append(f'<td class="right">{item["score_string"][stage]}</td>')
			rows.append(f'<tr>{"".join(row)}</tr>')
		return ''.join(rows)
	
	def html(self):
		self.generate_table()
		return f'{self.name}<table>{self.html_header()}{self.html_table()}</table>'
	
	def generate_table(self):
		self.data = []
		
		stages = [self.stages[stage] for stage in self.stages]
		
		for shooter in self.shooters:
			item = {}
			item['name'] = self.shooters[shooter].name()
			item['division'] = self.shooters[shooter].short_division
			item['score'] = {}
			item['score_string'] = {}
			for stage in stages:
				item['score'][stage.id] = self.shooters[shooter].score(stage)
				item['score_string'][stage.id] = self.shooters[shooter].score_string(stage)
			item['total'] = self.shooters[shooter].total(stages)
			item['total_string'] = self.shooters[shooter].total_string(stages)
			self.data.append(item)

class IPSCMatch(Match):
	def generate_table(self):
		self.data = []
		
		stages = [self.stages[stage] for stage in self.stages]
		
		for shooter in self.shooters:
			item = {}
			item['name'] = self.shooters[shooter].name()
			item['division'] = self.shooters[shooter].short_division
			item['score'] = {}
			item['score_string'] = {}
			for stage in stages:
				item['score'][stage.id] = self.shooters[shooter].score(stage)
				item['score_string'][stage.id] = self.shooters[shooter].score_string(stage)
			item['total'] = self.shooters[shooter].total(stages)
			item['total_string'] = self.shooters[shooter].total_string(stages)
			self.data.append(item)
	
	def html_header(self):
		return '<tr><th>'+'</th><th>'.join(['#', 'Name', 'Div.', 'Total', ''])+'</th><th>'.join(('Stage {}<br><span style="font-size: x-small">{}</span>'.format(self.stages[stage].number, self.stages[stage].short_name) for stage in self.stages))+'</th></tr>'
	
	def html_table(self):
		self.generate_table()
		html = ''
		for index, item in enumerate(sorted(self.data, key=lambda x: x['total'], reverse=True)):
			html += '<tr><td style="text-align: center">'
			html += '</td><td style="text-align: center">'.join(['{}'.format(index+1), item['name'], item['division'], item['total_string']]) + '</td><td>'
			html += '</td><td>'.join((item['score_string'][stage] for stage in self.stages))
			html += '</td></tr>'
		return html + '</table>'
	
	def html(self):
		return f'{self.name}<table>{self.html_header()}{self.html_table()}'

class NRAMatch(Match):
	def generate_table(self):
		self.data = []
		
		stages = [self.stages[stage] for stage in self.stages]
		
		for shooter in self.shooters:
			item = {}
			item['name'] = self.shooters[shooter].name()
			item['division'] = self.shooters[shooter].short_division
			item['score'] = {}
			item['score_string'] = {}
			for stage in stages:
				item['score'][stage.id] = self.shooters[shooter].score(stage)
				item['score_string'][stage.id] = self.shooters[shooter].score_string(stage)
			item['total'] = self.shooters[shooter].total(stages)
			item['total_string'] = self.shooters[shooter].total_string(stages)
			self.data.append(item)
	
	def html_header(self):
		return '<tr><th>'+'</th><th>'.join(['#', 'Name', 'Div.', 'Total', ''])+'</th><th>'.join(('Stage {}<br><span style="font-size: x-small">{}</span>'.format(self.stages[stage].number, self.stages[stage].short_name) for stage in self.stages))+'</th></tr>'
	
	def html_table(self):
		self.generate_table()
		html = ''
		for index, item in enumerate(sorted(self.data, key=lambda x: x['total'], reverse=True)):
			html += '<tr><td style="text-align: center">'
			html += '</td><td style="text-align: center">'.join(['{}'.format(index+1), item['name'], item['division'], item['total_string']]) + '</td><td>'
			html += '</td><td>'.join((item['score_string'][stage] for stage in self.stages))
			html += '</td></tr>'
		return html + '</table>'
		
	def html(self):
		return f'{self.name}<table>{self.html_header()}{self.html_table()}'

class Stage:
	def __init__(self, match, match_stage):
		self.match = match
		self.id = match_stage['stage_uuid']
		self.update(match_stage)
	
	def update_if_modified(self, match_stage):
		modified_date = match_stage['stage_modifieddate']
		if modified_date != self.modified_date:
			modified_time_1 = datetime.datetime.strptime(modified_date,'%Y-%m-%d %H:%M:%S.%f')
			modified_time_2 = datetime.datetime.strptime(self.modified_date,'%Y-%m-%d %H:%M:%S.%f')
			if modified_time_1 > modified_time_2:
				self.update(match_stage)
	
	def update(self, match_stage):
		self.name = match_stage['stage_name']
		if 'StageNameSubstitutions' in CONFIG and self.name in CONFIG['StageNameSubstitutions']:
			self.short_name = textwrap.shorten(CONFIG['StageNameSubstitutions'][match_stage['stage_name']], width=12, placeholder='...')
		else:
			self.short_name = textwrap.shorten(match_stage['stage_name'], width=12, placeholder='...')
		self.number = match_stage['stage_number']
		self.modified_date = match_stage['stage_modifieddate']
	
	def __str__(self):
		return self.name
	
	def __repr__(self):
		return '<{} "{}">'.format(self.__class__.__name__, self.name)

class SCSAStage(Stage):
	def update(self, match_stage):
		super().update(match_stage)
		self.remove_worst_string = match_stage['stage_removeworststring']
		self.strings = match_stage['stage_strings']
		self.max_time = 30 * (self.strings - self.remove_worst_string)

class IPSCStage(Stage):
	def update(self, match_stage):
		super().update(match_stage)

class NRAStage(Stage):	
	def update(self, match_stage):
		super().update(match_stage)
		self.custom_targets = match_stage['stage_customtargets']

class StageScore:
	def __init__(self, match, stage_id, stage_stagescore):
		self.match = match
		self.stage_id = stage_id
		self.shooter_id = stage_stagescore['shtr']
		self.update(stage_stagescore)
	
	def update(self, stage_stagescore):
		# Android Tablet doesn't always mark completed scores as 'approved'
		#if 'aprv' in stage_stagescore:
		#	self.approved = stage_stagescore['aprv']
		#else:
		#	self.approved = False
		if 'dnf' in stage_stagescore:
			self.dnf = stage_stagescore['dnf']
		else:
			self.dnf = False
		self.modified_date = stage_stagescore['mod']
		
	def update_if_modified(self, stage_stagescore):
		modified_date = stage_stagescore['mod']
		if stage_stagescore['mod'] != self.modified_date:
			modified_time_1 = datetime.datetime.strptime(modified_date,'%Y-%m-%d %H:%M:%S.%f')
			modified_time_2 = datetime.datetime.strptime(self.modified_date,'%Y-%m-%d %H:%M:%S.%f')
			if modified_time_1 > modified_time_2:
				self.update(stage_stagescore)
	
	def __str__(self):
		return '{:.2f}'.format(self.total)
	
	def __repr__(self):
		return '<{} {:.2f}>'.format(self.__class__.__name__, self.total)

class SCSAStageScore(StageScore):
	def __init__(self, match, stage_id, stage_stagescore):
		self.penalties_value = match.penalties_value
		super().__init__(match, stage_id, stage_stagescore)
	
	def update(self, stage_stagescore):
		super().update(stage_stagescore)
		self.strings = numpy.array(stage_stagescore['str'])
		if 'penss' in stage_stagescore:
			self.penalty_array = numpy.array(stage_stagescore['penss'])
			self.penalties = [sum(penalties) for penalties in self.penalty_array*self.penalties_value]
			self.strings_with_penalties = self.strings + self.penalties
		else:
			self.strings_with_penalties = self.strings
		limit = self.strings_with_penalties.clip(None, 30)
		worst = max(limit)
		self.total = sum(limit)-worst

class IPSCStageScore(StageScore):
	def update(self, stage_stagescore):
		super().update(stage_stagescore)
		time = sum(stage_stagescore['str'])
		if time == 0:
			self.total = 0
			self.total_string = '0'
		else:
			self.total = stage_stagescore['rawpts']/time
			self.total_string = f'{self.total:.4f} ({stage_stagescore["rawpts"]}/{time})'

class NRAStageScore(StageScore):
	def update(self, stage_stagescore):
		super().update(stage_stagescore)
		self.total = 0
		if self.stage_id in self.match.stages:
			for i, stage_customtarget in enumerate(self.match.stages[self.stage_id].custom_targets):
				if 'cts' in stage_stagescore and len(stage_stagescore['cts']) > i:
					for j, target_targdesc in enumerate(stage_customtarget['target_targdesc']):
						self.total += stage_stagescore['cts'][i][j]*float(target_targdesc[1])

class Shooter:
	def __init__(self, match_shooter):
		self.id = match_shooter['sh_uid']
		self.update(match_shooter)
		self.scores = {}
	
	def update_if_modified(self, match_shooter):
		modified_date = match_shooter['sh_mod']
		if match_shooter['sh_mod'] != self.modified_date:
			modified_time_1 = datetime.datetime.strptime(modified_date,'%Y-%m-%d %H:%M:%S.%f')
			modified_time_2 = datetime.datetime.strptime(self.modified_date,'%Y-%m-%d %H:%M:%S.%f')
			if modified_time_1 > modified_time_2:
				self.update(match_shooter)
	
	def update(self, match_shooter):
		self.firstname = match_shooter['sh_fn']
		self.lastname = match_shooter['sh_ln']
		self.division = match_shooter['sh_dvp']
		if 'DivisionNameSubstitutions' in CONFIG and self.division in CONFIG['DivisionNameSubstitutions']:
			self.short_division = CONFIG['DivisionNameSubstitutions'][match_shooter['sh_dvp']]
		else:
			self.short_division = self.division
		self.deleted = match_shooter['sh_del']
		self.modified_date = match_shooter['sh_mod']
		self.disqualified = match_shooter['sh_dq']
		self.deleted = match_shooter['sh_del']
	
	def name(self):
		return '{} {}'.format(self.firstname, self.lastname)
	
	def __repr__(self):
		return '<{} "{}", "{}", "{}">'.format(self.__class__.__name__, self.firstname, self.lastname, self.division)

class SCSAShooter(Shooter):
	def score(self, stage):
		if not self.disqualified and stage.id in self.scores:
			score = self.scores[stage.id]
			if score.total != 0 and not score.dnf:
				return score.total
		return stage.max_time
	
	def score_string(self, stage):
		if stage.id in self.scores:
			score = self.scores[stage.id]
			if score.dnf:
				return 'DNF'
			if score.total != 0:
				return '{:.2f}'.format(score.total)
		return '-'
	
	def total(self, stages):
		return sum((self.score(stage) for stage in stages))
	
	def total_string(self, stages):
		if self.disqualified:
			return 'DQ'
		return '{:.2f}'.format(self.total(stages))

class IPSCShooter(Shooter):
	def score(self, stage):
		if not self.disqualified and stage.id in self.scores:
			score = self.scores[stage.id]
			if score.total != 0 and not score.dnf:
				return score.total
	
	def score_string(self, stage):
		if not self.disqualified and stage.id in self.scores:
			score = self.scores[stage.id]
			if score.dnf:
				return 'DNF'
			if score.total != 0:
				return score.total_string
		return '-'
	
	def total(self, stages):
		return 0
	
	def total_string(self, stages):
		return '-'

class NRAShooter(Shooter):
	def score(self, stage):
		if not self.disqualified and stage.id in self.scores:
			score = self.scores[stage.id]
			if not score.dnf:
				return score.total
		return 0
	
	def score_string(self, stage):
		if stage.id in self.scores:
			score = self.scores[stage.id]
			if score.dnf:
				return 'DNF'
			return '{:n}'.format(score.total)
		return '-'
	
	def total(self, stages):
		return sum((self.score(stage) for stage in stages))
	
	def total_string(self, stages):
		if self.disqualified:
			return 'DQ'
		return '{:n}'.format(self.total(stages))

CONFIG = configparser.RawConfigParser(delimiters='=')
CONFIG.read('ps-config.ini')
kiosk = Kiosk()
kiosk.loop()