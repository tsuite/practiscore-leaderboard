#!venv/bin/python3

import flask
import configobj
import configobj.validate
import asyncio
import socket
import uuid
import time
import struct
import zlib
from apscheduler.schedulers.background import BackgroundScheduler

import sys
import json
import argparse

app = flask.Flask(__name__)

__version__ = '1.0.0-alpha'
print(f'practiscore-leaderboard-{__version__}')


@app.get('/')
def get_index():
	data = kiosk.data()
	for match in data['matches']:
		for division in match['divisions']:
				match['divisions'][division] = sorted(match['divisions'][division], key=lambda x: x['match_points_total'], reverse=True)
	return flask.render_template('index.html', data=data, version=__version__)

@app.get('/match/<match_id>')
def get_match(match_id):
	data = kiosk.match_data(match_id)
	if data['match']:
		for division in data['match']['divisions']:
			data['match']['divisions'][division] = sorted(data['match']['divisions'][division], key=lambda x: x['match_points_total'], reverse=True)
		return flask.render_template('match.html', data=data, version=__version__)
	return flask.redirect('/', code=302)

@app.get('/match/<match_id>/stage/<stage_id>')
def get_stage(match_id, stage_id):
	data = kiosk.stage_data(match_id, stage_id)
	if data['match'] and data['stage']:
		if data['match']['match_id'] == match_id:
			for division in data['match']['divisions']:
				data['match']['divisions'][division] = sorted(data['match']['divisions'][division], key=lambda x: x['match_points'][stage_id], reverse=True)
			return flask.render_template('stage.html', data=data, version=__version__)
	return flask.redirect('/', code=302)

@app.get('/json')
def get_json():
	return kiosk.data()

class Kiosk:
	def __init__(self):
		configvalidator = configobj.validate.Validator()
		configspec = configobj.ConfigObj('configspec.ini', list_values=False, file_error=True, interpolation=False)
		config = configobj.ConfigObj('config.ini', file_error=True, configspec=configspec, interpolation=False)
		configvalidation = config.validate(configvalidator, preserve_errors=True)
		if configvalidation != True:
			for entry in configobj.flatten_errors(config, configvalidation):
				section_list, key, error = entry
				if key is not None:
					section_list.append(key)
				else:
						section_list.append('[missing section]')
				section_string = '.'.join(section_list)
				if error == False:
					error = 'missing value or section.'
				raise ValueError (f'{config.filename}:{section_string}: {error}')
		
		if 'device_uuid' in config:
			self.device_uuid = config['device_uuid']
		else:
			self.device_uuid = str(uuid.uuid4())
		if 'match_uuid' in config:
			self.match_uuid = config['match_uuid']
		else:
			self.match_uuid = str(uuid.uuid4())
		self.division_name_substitutions = config.get('DivisionNameSubstitutions', {})
		self.stage_name_substitutions = config.get('StageNameSubstitutions', {})
		
		self.devices = {}
		if 'Devices' in config:
			for device_name in config['Devices']:
				self.devices[device_name] = PSDevice(device_name, config['Devices'][device_name], self.device_uuid, self.match_uuid)
		if 'DummyDevices' in config:
			for device_name in config['DummyDevices']:
				self.devices[device_name] = JSONDevice(device_name, config['DummyDevices'][device_name])
	
		for device_name in self.devices:
			device = self.devices[device_name]
			if device.match_def:
				match_def = device.match_def
	def data(self):
		matches = self.matches()
		return {'matches': [matches[id].data() for id in matches], 'devices': [self.devices[id].data() for id in self.devices]}
	def match_data(self, match_id):
		return {'match': self.match(match_id).data(), 'devices': [self.devices[id].data() for id in self.devices]}
	def stage_data(self, match_id, stage_id):
		return {'match': self.match(match_id).data(), 'stage': self.match(match_id).stage(stage_id).data(), 'devices': [self.devices[id].data() for id in self.devices]}
	def matches(self):
		matches = {}
		for device_name in self.devices:
			match_def = self.devices[device_name].match_def
			match_scores = self.devices[device_name].match_scores
			match_subtype = match_def.get('match_subtype', '')
			match_id = match_def.get('match_id', '')
			if match_id in matches:
				matches[match_id].update(match_def, match_scores)
			else:
				if match_subtype == 'ipsc':
					matches[match_id] = IPSCMatch(match_def, match_scores)
		return matches
	def match(self, match_id):
		matches = self.matches()
		return matches.get(match_id)
	def start(self):
		self.scheduler = BackgroundScheduler()
		for device_name in self.devices:
			device = self.devices[device_name]
			try:
				self.scheduler.add_job(device.start, 'interval', seconds=device.poll_time)
			except Exception:
				pass
		self.scheduler.start()

class Device:
	def __init__(self, name, config):
		self.name = name
		self.match_def_path = config.get('match_def_path')
		self.match_scores_path = config.get('match_scores_path')
		self.poll_time = config.get('poll_time')
		
	def start(self):
		self.update()
	
	def update(self):
		raise NotImplementedError
	
	def data(self):
		return {'name': self.name}

class JSONDevice(Device):
	def __init__(self, name, config):
		super().__init__(name, config)
		self.match_def = {}
		self.match_scores = {}
		#asyncio.run(self.update())
		
	def update(self):
		if self.match_def_path:
			try:
				with open(self.match_def_path, 'r') as f:
					self.match_def = json.load(f)
			except FileNotFoundError:
				pass
		if self.match_scores_path:
			try:
				with open(self.match_scores_path, 'r') as f:
					self.match_scores = json.load(f)
			except FileNotFoundError:
				pass

class PSDevice(Device):
	SIGNATURE = 0x19113006
	FLAGS_ANDROID = 4
	FLAGS_IOS = 3
	#MSG_STATUS_REQUEST = 6
	#MSG_STATUS_RESPONSE = 7
	MSG_MATCH_REQUEST = 8
	MSG_MATCH_RESPONSE = 9
	task = None

	def __init__(self, name, config, device_uuid, match_uuid):
		super().__init__(name, config)
		self.hostname = socket.gethostname()
		self.address = config.get('address')
		self.port = config.get('port')
		self.timeout = config.get('timeout')
		self.slow_poll = config.get('slow_poll')
		self.shutdown = config.get('shutdown')
		
		self.match_def = {}
		self.match_scores = {}
	
	def update(self):
		#if not self.task or self.task.done():
		#	self.task = asyncio.create_task(self.temp())
		#while True:
		try:
			asyncio.run(asyncio.wait_for(self.temp(), timeout=self.timeout))
		except asyncio.exceptions.TimeoutError:
			print(f'{self.name}: Timeout Error')
		except OSError:
			print(f'{self.name}: OSError')
		#	await asyncio.sleep(self.poll_time)
		#await asyncio.wait_for(self.temp(), timeout=self.timeout)
		
	async def temp(self):
		reader, writer = await asyncio.open_connection(self.address, self.port)
		
		tx_data = struct.pack('!IIIII', self.SIGNATURE, 0, self.MSG_MATCH_REQUEST, self.FLAGS_ANDROID, int(time.time()))
		writer.write(tx_data)
		await writer.drain()
		
		rx_header = await reader.readexactly(20)
		
		(f_signature, f_length, f_type, f_flags, f_time) = struct.unpack('!IIIII', rx_header)
		f_signature_err = f_signature != self.SIGNATURE
		f_type_err = f_type != self.MSG_MATCH_RESPONSE
		f_flags_err = f_flags != self.FLAGS_ANDROID and f_flags != self.FLAGS_IOS
		if f_signature_err or f_type_err or f_flags_err:
			raise self.PSInvalidHeader
		
		match_def_length = struct.unpack('!I',await reader.readexactly(4))[0]
		match_scores_length = f_length - match_def_length - 4
		
		match_def = json.loads(zlib.decompress(await reader.readexactly(match_def_length)))
		if match_def:
			self.match_def = match_def
			if self.match_def_path:
				with open(self.match_def_path, 'w') as f:
					f.write(json.dumps(match_def))
		if match_scores_length:
			match_scores = json.loads(zlib.decompress(await reader.readexactly(match_scores_length)))
			if match_scores:
				self.match_scores = match_scores
				if self.match_scores_path:
					with open(self.match_scores_path, 'w') as f:
						f.write(json.dumps(match_scores))
		writer.close()
		await writer.wait_closed()
		
	class PSInvalidHeader(Exception):
		pass

class Match:
	def __init__(self, match_def, match_scores):
		self.match_id = match_def.get('match_id')
		self.match_subtype = match_def.get('match_subtype')
		self.shooters = {}
		self.stages = {}
		self.scores = {}
		self.update_match_data(match_def)
		self.update(match_def, match_scores)
	
	def update(self, match_def, match_scores):
		match_modifieddate = match_def['match_modifieddate']
		if match_modifieddate != self.match_modifieddate:
			match_modifieddate_1 = datetime.datetime.strptime(match_modifieddate,'%Y-%m-%d %H:%M:%S.%f')
			match_modifieddate_2 = datetime.datetime.strptime(self.match_modifieddate,'%Y-%m-%d %H:%M:%S.%f')
			if match_modifieddate_1 > match_modifieddate_2:
				self.update_match_data(match_def)
		if 'match_shooters' in match_def:
			self.update_shooters(match_def['match_shooters'])
		if 'match_stages' in match_def:
			self.update_stages(match_def['match_stages'])
		if 'match_scores' in match_scores:
			self.update_scores(match_scores['match_scores'])
		self.match_name = match_def.get('match_name', '')
		
	def update_match_data(self, match_def):
		self.match_name = match_def.get('match_name')
		self.match_modifieddate = match_def.get('match_modifieddate')
		
	def update_scores(self, match_scores):
		for stage in match_scores:
			for stage_stagescore in stage['stage_stagescores']:
				shooter_id = stage_stagescore['shtr']
				stage_id = stage['stage_uuid']
				if stage_id not in self.scores:
					self.scores[stage_id] = {}
				if shooter_id in self.scores[stage_id]:
					self.scores[stage_id][shooter_id].update_if_modified(stage_stagescore)
				elif self.match_subtype == 'scsa':
					self.scores[stage_id][shooter_id] = SCSAStageScore(self, stage_id, stage_stagescore)
				elif self.match_subtype == 'nra':
					self.scores[stage_id][shooter_id] = NRAStageScore(self, stage_id, stage_stagescore)
				elif self.match_subtype == 'ipsc':
					self.scores[stage_id][shooter_id] = IPSCStageScore(self, stage_id, stage_stagescore)
	
	def update_stage(self, match_stage):
		if match_stage['stage_uuid'] in self.stages:
			self.stages[match_stage['stage_uuid']].update_if_modified(match_stage)
		elif self.match_subtype == 'scsa':
			self.stages[match_stage['stage_uuid']] = SCSAStage(self, match_stage)
		elif self.match_subtype == 'nra':
			self.stages[match_stage['stage_uuid']] = NRAStage(self, match_stage)
		elif self.match_subtype == 'ipsc':
			self.stages[match_stage['stage_uuid']] = IPSCStage(self, match_stage)
		else:
			self.stages[match_stage['stage_uuid']] = Stage(self, match_stage)
	
	def update_stages(self, match_stages):
		for match_stage in match_stages:
			self.update_stage(match_stage)
	
	def update_shooter(self, match_shooter):
		if match_shooter['sh_uid'] in self.shooters:
			self.shooters[match_shooter['sh_uid']].update_if_modified(match_shooter)
		elif self.match_subtype == 'scsa':
			self.shooters[match_shooter['sh_uid']] = SCSAShooter(self, match_shooter)
		elif self.match_subtype == 'nra':
			self.shooters[match_shooter['sh_uid']] = NRAShooter(self, match_shooter)
		elif self.match_subtype == 'ipsc':
			self.shooters[match_shooter['sh_uid']] = IPSCShooter(self, match_shooter)
		else:
			self.shooters[match_shooter['sh_uid']] = Shooter(self, match_shooter)
	
	def update_shooters(self, match_shooters):
		for match_shooter in match_shooters:
			self.update_shooter(match_shooter)
	
	def stage_list(self):
		list = [id for id in self.stages if not self.stages[id].stage_deleted]
		return sorted(list, key=lambda x: (self.stages[x].stage_number, self.stages[x].id))
	def shooter_data(self):
		return [self.shooters[id].data() for id in self.shooters]
	def shooter_by_division(self):
		data = {}
		for id in self.shooters:
			shooter = self.shooters[id]
			if not shooter.division in data:
				data[shooter.division] = []
			data[shooter.division].append(shooter.data())
		return data
	def stage_data(self):
		for id in self.stages:
			self.stages[id].post_process()
		return [self.stages[id].data() for id in self.stages if not self.stages[id].stage_deleted]
	def stage(self, stage_id):
		return self.stages.get(stage_id)

class IPSCMatch(Match):
	def update_match_data(self, match_def):
		super().update_match_data(match_def)
		self.match_pfs = {pf['name'].lower():pf for pf in match_def.get('match_pfs')}
	def data(self):
		return {'match_name': self.match_name, 'match_id': self.match_id, 'match_pfs': self.match_pfs, 'stages':self.stage_data(), 'divisions':self.shooter_by_division()}

class Shooter:
	def __init__(self, match, match_shooter):
		self.id = match_shooter['sh_uid']
		self.match = match
		self.update(match_shooter)
		#self.stages = []
	
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
		if self.division in kiosk.division_name_substitutions:
			self.short_division = kiosk.division_name_substitutions[match_shooter['sh_dvp']]
		else:
			self.short_division = self.division
		self.deleted = match_shooter.get('sh_del', False)
		self.disqualified = match_shooter.get('sh_dq', False)
		self.modified_date = match_shooter['sh_mod']
	
	def name(self):
		return f'{self.firstname} {self.lastname}'

class IPSCShooter(Shooter):
	def update(self, match_shooter):
		super().update(match_shooter)
		self.pf = match_shooter.get('sh_pf', '')
	def score_string(self, stage):
		if not self.disqualified and stage.id in self.scores:
			score = self.scores[stage.id]
			if score.dnf:
				return 'DNF'
			if score.hit_factor != 0:
				return score.hit_factor_string
		return '-'
	def scores(self):
		self.match_points = {}
		self.match_points_text = {}
		self.hf = {}
		self.pts = {}
		self.time = {}
		if not self.disqualified:
			for stage_id in [id for id in self.match.stages if not self.match.stages[id].stage_deleted]:
				stage = self.match.stages[stage_id]
				max_hit_factor = stage.max_hit_factors.get(self.division,0)
				if stage_id in self.match.scores and self.id in self.match.scores[stage_id]:
					score = self.match.scores[stage_id][self.id]
					self.hf[stage_id] = score.hit_factor
					self.pts[stage_id] = score.pts
					self.time[stage_id] = score.time
					if score.dnf :
						self.match_points[stage_id] = 0
						self.match_points_text[stage_id] = 'DNF'
					elif score.time == 0:
						self.match_points[stage_id] = 0
						self.match_points_text[stage_id] = '-'
					elif score.hit_factor == 0 or max_hit_factor == 0:
						self.match_points[stage_id] = 0
						self.match_points_text[stage_id] = '0.0000'
					else:
						match_points = score.hit_factor/max_hit_factor*stage.max_points
						self.match_points[stage_id] = match_points
						self.match_points_text[stage_id] = f'{match_points:.4f}'
		self.match_points_total = sum(self.match_points[x] for x in self.match_points)
		self.match_points_total_text = f'{self.match_points_total:.4f}'
	def data(self):
		self.scores()
		return {'name': self.name(), 'short_division': self.short_division, 'match_points': self.match_points, 'match_points_text': self.match_points_text, 'match_points_total': self.match_points_total, 'match_points_total_text': self.match_points_total_text, 'hf':self.hf, 'pts':self.pts, 'time':self.time}
class Stage:
	def __init__(self, match, match_stage):
		self.match = match
		self.id = match_stage['stage_uuid']
		#self.shooters = []
		self.update(match_stage)
		
	def update_if_modified(self, match_stage):
		stage_modifieddate = match_stage['stage_modifieddate']
		if stage_modifieddate != self.stage_modifieddate:
			stage_modifieddate_1 = datetime.datetime.strptime(stage_modifieddate,'%Y-%m-%d %H:%M:%S.%f')
			stage_modifieddate_2 = datetime.datetime.strptime(self.stage_modifieddate,'%Y-%m-%d %H:%M:%S.%f')
			if stage_modifieddate_1 > stage_modifieddate_2:
				self.update(match_stage)
	
	def update(self, match_stage):
		self.stage_number = match_stage.get('stage_number')
		self.stage_name = match_stage.get('stage_name')
		self.stage_modifieddate = match_stage.get('stage_modifieddate')
		self.stage_deleted = match_stage.get('stage_deleted', False)

class IPSCStage(Stage):
	def __init__(self, match, match_stage):
		super().__init__(match, match_stage)
		self.max_hit_factor = 0
		self.max_hit_factors = {}
	
	def update(self, match_stage):
		super().update(match_stage)
		self.stage_poppers = match_stage.get('stage_poppers', 0)
		self.stage_targets = match_stage.get('stage_targets', [])
		self.stage_reqshots = self.stage_poppers + sum(stage_target.get('target_reqshots', 0) for stage_target in self.stage_targets)
		self.max_points = 5*self.stage_reqshots
	
	def post_process(self):
		self.max_hit_factor = 0
		self.max_hit_factors = {}
		for shooter_id in self.match.shooters:
			if self.id in self.match.scores and shooter_id in self.match.scores[self.id]:
				hit_factor = self.match.scores[self.id][shooter_id].hit_factor
				if shooter_id in self.match.shooters:
					division = self.match.shooters[shooter_id].division
					self.max_hit_factors[division] = max(self.max_hit_factors[division] if division in self.max_hit_factors else 0, hit_factor)
				self.max_hit_factor = max(self.max_hit_factor, hit_factor)
				
	
	def data(self):
		return {'stage_id': self.id, 'stage_number': self.stage_number, 'max_points': self.max_points, 'stage_reqshots': self.stage_reqshots, 'stage_poppers': self.stage_poppers, 'stage_targets': self.stage_targets, 'stage_deleted': self.stage_deleted, 'max_hit_factor': self.max_hit_factor, 'max_hit_factors': self.max_hit_factors}

class StageScore:
	def __init__(self, match, stage_id, stage_stagescore):
		self.match = match
		self.stage_id = stage_id
		self.shooter_id = stage_stagescore['shtr']
		self.update(stage_stagescore)
	
	def update(self, stage_stagescore):
		if 'dnf' in stage_stagescore:
			self.dnf = stage_stagescore['dnf']
		else:
			self.dnf = False
		self.modified_date = stage_stagescore['mod']
	
	def update_if_modified(self, stage_stagescore):
		modified_date = stage_stagescore['mod']
		if stage_stagescore['mod'] != self.modified_date:
			modified_date_1 = datetime.datetime.strptime(modified_date,'%Y-%m-%d %H:%M:%S.%f')
			modified_date_2 = datetime.datetime.strptime(self.modified_date,'%Y-%m-%d %H:%M:%S.%f')
			if modified_date_1 > modified_date_2:
				self.update(stage_stagescore)

class IPSCStageScore(StageScore):
	def update(self, stage_stagescore):
		super().update(stage_stagescore)
		score = {'A':0, 'B':0, 'C': 0, 'D': 0, 'M': 0, 'NS': 0, 'NPM': 0}
		pf = self.match.match_pfs.get(self.match.shooters[self.shooter_id].pf.lower(),{})
		proc_cnts = stage_stagescore.get('proc_cnts',[])
		if 'poph' in stage_stagescore:
			score['A'] = stage_stagescore['poph']
		if 'popns' in stage_stagescore:
			score['NS'] = -stage_stagescore['popns']
		if 'popm' in stage_stagescore:
			score['M'] = -stage_stagescore['popm']
		score['NS'] -= sum(sum(proc_cnt[x] for x in proc_cnt) for proc_cnt in proc_cnts)
		if 'ts' in stage_stagescore:
			for x in stage_stagescore['ts']:
				score['A'] += (x) & 0xf
				score['B'] += (x >> 4) & 0xf
				score['C'] += (x >> 8) & 0xf
				score['D'] += (x >> 12) & 0xf
				score['NS'] -= (x >> 16) & 0xf
				score['M'] -= (x >> 20) & 0xf
				score['NPM'] += (x >> 24) & 0xf
		self.pts = max(sum(pf[k]*score[k] for k in pf if k in score),0)
		self.time = sum(stage_stagescore['str'])
		if self.time == 0:
			self.hit_factor = 0
			self.hit_factor_string = '-'
		else:
			self.hit_factor = self.pts/self.time
			self.hit_factor_string = f'HF:{self.hit_factor:.4f}'

if __name__ == '__main__':
	kiosk = Kiosk()
	kiosk.start()
	app.run(host='0.0.0.0', debug=True)

exit()


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
		x = True
		while x:
			#x = False
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

class DummyDevice:
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
	
	def html(self):
		if 'match_name' in self.match_def:
			return f'<span class="device-status">{self.name}: {self.match_def["match_name"]}, {self.update_date} {"Online" if self.online else "Offline"} ({self.poll_counter + self.poll_time*self.slow_poll_counter})</span>'
		else:
			return f'<span class="device-status">{self.name}: {self.update_date} {"Online" if self.online else "Offline"} ({self.poll_counter + self.poll_time*self.slow_poll_counter})</span>'
	
	def poll(self):
		if self.poll_counter < 1 and self.slow_poll_counter < 1:
			self.poll_counter = self.poll_time
			try:
				with open(f'status_{self.name}.json', 'r') as f:
					self.status = json.load(f)
			except Exception:
				self.slow_poll_counter = max(self.slow_poll-1,0)
				self.online = False;
			try:
				with open(f'match_def_{self.name}.json', 'r') as f:
					self.match_def = json.load(f)
				with open(f'match_scores_{self.name}.json', 'r') as f:
					self.match_scores = json.load(f)
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
	def update_match_data(self, match_def):
		super().update_match_data(match_def)
		self.pfs = match_def['match_pfs']
	
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
		for index, item in enumerate(sorted(self.data, key=lambda x: x['division'])):
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
		
	def get_int(self, dict, key):
		if key in dict:
			return dict[key]
		return 0
	
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
		self.max_points = 5*(match_stage['stage_poppers']+sum([target['target_reqshots'] for target in match_stage['stage_targets']]))
		self.html_th = f'<th>Stage {self.number}<br><span style="font-size: x-small">Max Points {self.max_points}</span></th>'

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
		if 'ts' in stage_stagescore:
			a, b, c, d, ns, m, npm = [0]*7
			if 'poph' in stage_stagescore:
				a = stage_stagescore['poph']
			if 'popm' in stage_stagescore:
				m = stage_stagescore['popm']
			for x in stage_stagescore['ts']:
				a += (x) & 0xf
				b += (x >> 4) & 0xf
				c += (x >> 8) & 0xf
				d += (x >> 12) & 0xf
				ns += (x >> 16) & 0xf
				m += (x >> 20) & 0xf
				npm += (x >> 24) & 0xf
				self.pts = max(a*5+b*3+c*3+d-ns*10-m*10,0)
		else:
			self.pts = 0
		self.time = sum(stage_stagescore['str'])
		if self.time == 0:
			self.hit_factor = 0
			self.hit_factor_string = '0'
		else:
			self.hit_factor = self.pts/self.time
			self.hit_factor_string = f'HF:{self.hit_factor:.4f}'

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
		
	def get_int(self, dict, key):
		if key in dict:
			return dict[key]
		return 0
	
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
			if score.hit_factor != 0 and not score.dnf:
				return score.hit_factor
	
	def score_string(self, stage):
		if not self.disqualified and stage.id in self.scores:
			score = self.scores[stage.id]
			if score.dnf:
				return 'DNF'
			if score.hit_factor != 0:
				return score.hit_factor_string
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
