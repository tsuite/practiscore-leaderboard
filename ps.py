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
import datetime
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
	return flask.render_template('index.html', data=data, version=__version__, time=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

@app.get('/match/<match_id>')
def get_match(match_id):
	data = kiosk.match_data(match_id)
	if data['match']:
		for division in data['match']['divisions']:
			data['match']['divisions'][division] = sorted(data['match']['divisions'][division], key=lambda x: x['match_points_total'], reverse=True)
		return flask.render_template('match.html', data=data, version=__version__, time=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
	return flask.redirect('/', code=302)

@app.get('/match/<match_id>/stage/<stage_id>')
def get_stage(match_id, stage_id):
	data = kiosk.stage_data(match_id, stage_id)
	if data['match'] and data['stage']:
		if data['match']['match_id'] == match_id:
			for division in data['match']['divisions']:
				data['match']['divisions'][division] = sorted(data['match']['divisions'][division], key=lambda x: x['match_points'][stage_id], reverse=True)
			return flask.render_template('stage.html', data=data, version=__version__, time=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
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
			if not shooter.deleted and not shooter.disqualified:
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
		self.match_points_string = {}
		self.stage_percent = {}
		self.stage_percent_string = {}
		self.hit_factor_string = {}
		self.points_string = {}
		self.time_string = {}
		self.hits = {}
		self.penalties = {}
		if not self.disqualified:
			for stage_id in [id for id in self.match.stages if not self.match.stages[id].stage_deleted]:
				stage = self.match.stages[stage_id]
				max_hit_factor = stage.max_hit_factors.get(self.division,0)
				if stage_id in self.match.scores and self.id in self.match.scores[stage_id]:
					score = self.match.scores[stage_id][self.id]
					self.penalties[stage_id] = score.penalties
					self.time_string[stage_id] = score.time_string
					self.hits[stage_id] = score.hits
					self.penalties[stage_id] = score.penalties
					if score.dnf :
						self.stage_percent[stage_id] = 0
						self.stage_percent_string[stage_id] = 'DNF'
						self.match_points[stage_id] = 0
						self.match_points_string[stage_id] = 'DNF'
						self.time_string[stage_id] = 'DNF'
						self.hit_factor_string[stage_id] = 'DNF'
						self.points_string[stage_id] = 'DNF'
					elif score.time == 0:
						self.stage_percent[stage_id] = 0
						self.stage_percent_string[stage_id] = '-'
						self.match_points[stage_id] = 0
						self.match_points_string[stage_id] = '-'
						self.time_string[stage_id] = '-'
						self.hit_factor_string[stage_id] = '-'
						self.points_string[stage_id] = '-'
					elif score.hit_factor == 0 or max_hit_factor == 0:
						self.stage_percent[stage_id] = 0
						self.stage_percent_string[stage_id] = f'{0:.2f} %'
						self.match_points[stage_id] = 0
						self.match_points_string[stage_id] = f'{0:.4f}'
						self.time_string[stage_id] = f'{0:.2f}'
						self.hit_factor_string[stage_id] = f'{0:.4f}'
						self.points_string[stage_id] = f'{0}'
					else:
						hit_factor_ratio = score.hit_factor/max_hit_factor
						self.stage_percent[stage_id] = hit_factor_ratio*100
						self.stage_percent_string[stage_id] = f'{hit_factor_ratio*100:.2f} %'
						match_points = hit_factor_ratio*stage.max_points
						self.match_points[stage_id] = match_points
						self.match_points_string[stage_id] = f'{match_points:.4f}'
						self.time_string[stage_id] = f'{score.time:.2f}'
						self.hit_factor_string[stage_id] = f'{score.hit_factor:.4f}'
						self.points_string[stage_id] = f'{score.points}'
				else:
					self.stage_percent[stage_id] = 0
					self.stage_percent_string[stage_id] = '-'
					self.match_points[stage_id] = 0
					self.match_points_string[stage_id] = '-'
					self.time_string[stage_id] = '-'
					self.hit_factor_string[stage_id] = '-'
					self.points_string[stage_id] = '-'
					self.hits[stage_id] = {'A':'-', 'B':'-', 'C': '-', 'D': '-', 'M': '-', 'NS': '-', 'NPM': '-', 'Proc': '-'}
		self.match_points_total = sum(self.match_points[x] for x in self.match_points)
		self.match_points_total_string = f'{self.match_points_total:.4f}'
	def data(self):
		self.scores()
		return {'name': self.name(),
			'short_division': self.short_division,
			'hits': self.hits,
			'hit_factor_string': self.hit_factor_string,
			'match_points': self.match_points,
			'match_points_string': self.match_points_string,
			'match_points_total': self.match_points_total,
			'match_points_total_string': self.match_points_total_string,
			'points_string':self.points_string,
			'time_string':self.time_string,
			'stage_percent':self.stage_percent,
			'stage_percent_string':self.stage_percent_string,
			'penalties':self.penalties}

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
			if not self.match.shooters[shooter_id].deleted and not self.match.shooters[shooter_id].disqualified:
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
		self.raw_points = stage_stagescore.get('rawpts', 0)
		hits = {'A':0, 'B':0, 'C': 0, 'D': 0, 'M': 0, 'NS': 0, 'NPM': 0, 'Proc': 0}
		points = ('A', 'B', 'C', 'D')
		penalties = ('M', 'NS')
		pf = self.match.match_pfs.get(self.match.shooters[self.shooter_id].pf.lower(),{})
		proc_cnts = stage_stagescore.get('proc_cnts',[])
		hits['A'] = stage_stagescore.get('poph', 0)
		hits['NS'] = stage_stagescore.get('popns', 0)
		hits['M'] = stage_stagescore.get('popm', 0)
		hits['Proc'] = sum(sum(proc_cnt[x] for x in proc_cnt) for proc_cnt in proc_cnts)
		if 'ts' in stage_stagescore:
			for x in stage_stagescore['ts']:
				hits['A'] += (x) & 0xf
				hits['B'] += (x >> 4) & 0xf
				hits['C'] += (x >> 8) & 0xf
				hits['D'] += (x >> 12) & 0xf
				hits['NS'] += (x >> 16) & 0xf
				hits['M'] += (x >> 20) & 0xf
				hits['NPM'] += (x >> 24) & 0xf
		self.points = sum(pf[k]*hits[k] for k in pf if k in points)
		self.penalties = sum(pf[k]*hits[k] for k in pf if k in penalties)+10*hits['Proc']
		self.time = sum(stage_stagescore['str'])
		self.time_string = f'{self.time:.2f}'
		self.hits = hits
		if self.time == 0:
			self.hit_factor = 0
			self.hit_factor_string = '-'
		else:
			self.hit_factor = max(self.points-self.penalties, 0)/self.time
			self.hit_factor_string = f'{self.hit_factor:.4f}'

if __name__ == '__main__':
	kiosk = Kiosk()
	kiosk.start()
	app.run(host='0.0.0.0', debug=True)
