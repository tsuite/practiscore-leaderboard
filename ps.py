#!/usr/bin/python3

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
import textwrap

class Kiosk:
	style = '<style>.name, .left{text-align: left} .total, .score, .right{text-align: right} .place, .header, .division, .center{text-align: center} html {font-family: "Helvetica"; font-size: large } tr:nth-child(even) {background: #DDD} td{padding: 2px 10px} th{padding: 2px 10px} .device-status{color: #CCC} </style>'
	
	def __init__(self):
		self.device_config = configparser.RawConfigParser()
		self.device_config.read('ps-devices.ini')
		self.devices = {}
		index = 0
		for poll_offset, name in enumerate(self.device_config.sections()):
			self.devices[name] = Device(self.device_config[name], poll_offset)
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
	def __init__(self, device_data, poll_offset):
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
		self.client = PSClient(self.address, self.port, self.timeout)
	
	def html(self):
		if 'match_name' in self.match_def:
			return f'<span class="device-status">{self.name}: {self.match_def["match_name"]}, {self.update_date} {"Online" if self.online else "Offline"} ({self.poll_counter + self.poll_time*self.slow_poll_counter})</span>'
		else:
			return f'<span class="device-status">{self.name}: {self.update_date} {"Online" if self.online else "Offline"} ({self.poll_counter + self.poll_time*self.slow_poll_counter})</span>'
	
	def poll(self):
		if self.poll_counter < 1 and self.slow_poll_counter < 1:
			self.poll_counter = self.poll_time
			try:
				self.status = self.client.read_status()
				with open(f'/mnt/ramdisk/status_{self.name}.json', 'w') as f:
					f.write(json.dumps(self.status))
			except Exception:
				self.slow_poll_counter = max(self.slow_poll-1,0)
				self.online = False;
			if self.shutdown != '' and 'ps_matchid' in self.status:
				if self.status['ps_matchid'] == self.shutdown:
					os.system('/usr/bin/sudo /usr/sbin/shutdown -h now')
			try:
				(self.match_def, self.match_scores) = self.client.read_match()
				with open(f'/mnt/ramdisk/match_def_{self.name}.json', 'w') as f:
					f.write(json.dumps(self.match_def))
				with open(f'/mnt/ramdisk/match_scores_{self.name}.json', 'w') as f:
					f.write(json.dumps(self.match_scores))
			except Exception:
				self.slow_poll_counter = max(self.slow_poll-1,0)
				self.online = False;
			else:
				self.update_date = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
				self.online = True;
		elif self.poll_counter < 1:
			self.slow_poll_counter -= 1
			self.poll_counter = self.poll_time
		else:
			self.poll_counter -= 1
	
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
		return '<tr><th class="header">'+'</th><th class="header">'.join(['#', 'Name', 'Div.', 'Total', ''])+'</th><th class="header">'.join(('Stage {}<br><span style="font-size: x-small">{}</span>'.format(self.stages[stage].number, self.stages[stage].short_name) for stage in self.stages))+'</th></tr>'
	
	def html_table(self):
		rows = []
		for index, item in enumerate(sorted(self.data, key=lambda x: x['total'])):
			row = []
			row.append(f'<td class="place">{index+1}</td><td class="name">{item["name"]}</td><td class="division">{item["division"]}</td><td class="total">{item["total_string"]}</td>')
			for stage in self.stages:
				row.append(f'<td class="score">{item["score_string"][stage]}</td>')
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
		
		for shooter in self.shooters:
			item = {}
			item['name'] = self.shooters[shooter].name()
			item['division'] = self.shooters[shooter].short_division
			item['score'] = {}
			item['score_string'] = {}
			for stage in self.stages:
				if not self.stages[stage].deleted:
					item['score'][self.stages[stage].id] = self.shooters[shooter].score(self.stages[stage])
					item['score_string'][self.stages[stage].id] = self.shooters[shooter].score_string(self.stages[stage])
			item['total'] = self.shooters[shooter].total(self.stages)
			item['total_string'] = self.shooters[shooter].total_string(self.stages)
			self.data.append(item)
	
	def html_header(self):
		header = []
		header.append('<tr>')
		header.append('<th>#</th>')
		header.append('<th>Name</th>')
		header.append('<th>Div.</th>')
		header.append('<th>Total</th>')
		for stage in self.stages:
			header.append(self.stages[stage].html_th)
		header.append('</tr>')
		return ''.join(header)
	
	def html_table(self):
		self.generate_table()
		html = ''
		for index, item in enumerate(sorted(self.data, key=lambda x: x['total'], reverse=True)):
			html += '<tr><td style="text-align: center">'
			html += '</td><td style="text-align: center">'.join(['{}'.format(index+1), item['name'], item['division'], item['total_string']]) + '</td><td>'
			html += '</td><td>'.join((item['score_string'][stage] for stage in item['score_string']))
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
		rows = []
		for index, item in enumerate(sorted(self.data, key=lambda x: x['total'], reverse=True)):
			row = []
			row.append(f'<td class="place">{index+1}</td><td class="name">{item["name"]}</td><td class="division">{item["division"]}</td><td class="center">{item["total_string"]}</td>')
			for stage in self.stages:
				row.append(f'<td class="center">{item["score_string"][stage]}</td>')
			rows.append(f'<tr>{"".join(row)}</tr>')
		return ''.join(rows)
		
	def html(self):
		return f'{self.name}<table>{self.html_header()}{self.html_table()}</table>'

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
		self.deleted = self.get_bool(match_stage, 'stage_deleted')
		self.html_th = f'<th>Stage {self.number}<br><span style="font-size: x-small">{self.short_name}</span></th>'
		self.modified_date = match_stage['stage_modifieddate']
	
	def get_bool(self, dict, key):
		if key in dict:
			return dict[key]
		return False
		
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
	pass

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
		self.deleted = self.get_bool(match_shooter, 'sh_del')
		self.disqualified = self.get_bool(match_shooter,'sh_dq')
		self.modified_date = match_shooter['sh_mod']
	
	def get_bool(self, dict, key):
		if key in dict:
			return dict[key]
		return False
	
	def name(self):
		return f'{self.firstname} {self.lastname}'

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

class PSClient:
	SIGNATURE = 0x19113006
	FLAGS_ANDROID = 4
	FLAGS_IOS = 3
	MSG_STATUS_REQUEST = 6
	MSG_STATUS_RESPONSE = 7
	MSG_MATCH_REQUEST = 8
	MSG_MATCH_RESPONSE = 9
	
	class _NetworkError(Exception):
		def __init__(self, message):
			self.message = message
	
	def __init__(self, host, port, timeout):
		self.host = host
		self.port = port
		self.timeout = timeout
		self.hostname = socket.gethostname()
		self.sock = socket.socket()
		sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		sock.settimeout(0)
		try:
			# doesn't even have to be reachable
			sock.connect((self.host, self.port))
			self.ip = sock.getsockname()[0]
		except Exception:
			self.ip = '127.0.0.1'
		finally:
			sock.close()
		
		self.device_status = {'ps_name': self.hostname,
			'ps_port': self.port,
			'ps_host': self.ip,
			'ps_matchname': 'Kiosk Display',
			'ps_matchid': str(uuid.uuid4()),
			'ps_modified': time.strftime('%Y-%m-%d %H:%M:%S.000'),
			'ps_battery': 100,
			'ps_uniqueid': str(uuid.uuid4())}
	
	def __del__(self):
		self.close()
	
	def is_open(self):
		return self.sock.fileno() > 0
	
	def open(self):
		if self.is_open():
			self.close()
		for res in socket.getaddrinfo(self.host, self.port, socket.AF_UNSPEC, socket.SOCK_STREAM):
			af, sock_type, proto, canon_name, sa = res
			try:
				self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
				self.sock.settimeout(self.timeout)
				self.sock.connect((self.host, self.port))
			except socket.error:
				self.sock.close()
		if not self.is_open():
			raise PSClient._NetworkError('connection refused')
	
	def close(self):
		self.sock.close()
	
	def read_status(self):
		self.device_status['ps_modified'] = time.strftime('%Y-%m-%d %H:%M:%S.000')
		tx_data = json.dumps(self.device_status)
		rx_data = self._req_data(self.MSG_STATUS_REQUEST, self.MSG_STATUS_RESPONSE, tx_data)
		return json.loads(rx_data)
	
	def read_match(self):
		self.device_status['ps_modified'] = time.strftime('%Y-%m-%d %H:%M:%S.000')
		rx_data = self._req_data(self.MSG_MATCH_REQUEST, self.MSG_MATCH_RESPONSE)
		match_def_length = struct.unpack('!I',rx_data[0:4])[0]
		match_def = json.loads(zlib.decompress(rx_data[4:match_def_length+4]))
		if len(rx_data) > match_def_length + 4:
			match_scores = json.loads(zlib.decompress(rx_data[match_def_length+4:]))
		else:
			match_scores = ''
		return (match_def, match_scores)
	
	def _send_data(self, tx_type, tx_data):
		tx_frame = self._add_header(tx_type, tx_data)
		#if not self.is_open():
		self.open()
		self.sock.send(tx_frame)
	
	def _recv(self, size):
		try:
			r_buffer = self.sock.recv(size)
		except socket.timeout:
			self.sock.close()
			raise PSClient._NetworkError('timeout error')
		except socket.error:
			r_buffer = b''
		if not r_buffer:
			self.sock.close()
			raise PSClient._NetworkError('recv error')
		return r_buffer
	
	def _recv_all(self, size):
		r_buffer = b''
		while len(r_buffer) < size:
			r_buffer += self._recv(size - len(r_buffer))
		return r_buffer
	
	def _recv_data(self, rx_type):
		rx_header = self._recv_all(20)
		(f_signature, f_length, f_type, f_flags, f_time) = struct.unpack('!IIIII',rx_header)
		f_signature_err = f_signature != self.SIGNATURE
		f_type_err = f_type != rx_type
		f_flags_err = f_flags != self.FLAGS_ANDROID and f_flags != self.FLAGS_IOS
		if f_signature_err or f_type_err or f_flags_err:
			self.close()
			raise PSClient._NetworkError('header checking error')
		rx_data = self._recv_all(f_length)
		self.close()
		return rx_data
	
	def _add_header(self, tx_type, tx_data):
		tx_header = struct.pack('!IIIII', self.SIGNATURE, len(tx_data), tx_type, self.FLAGS_ANDROID, int(time.time()))
		return tx_header + tx_data.encode()
	
	def _req_data(self, tx_type, rx_type, tx_data = ''):
		self._send_data(tx_type, tx_data)
		return self._recv_data(rx_type)

CONFIG = configparser.RawConfigParser(delimiters='=')
CONFIG.read('ps-config.ini')
kiosk = Kiosk()
kiosk.loop()