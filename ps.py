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
import os

app = flask.Flask(__name__)

__version__ = '1.0.0-alpha'
print(f'practiscore-leaderboard-{__version__}')


@app.get('/2')
def get_index2():
	data = kiosk.data()
	for match in data['matches']:
		for division in match['divisions']:
				match['divisions'][division] = sorted(match['divisions'][division], key=lambda x: x['match_points_total'], reverse=True)
	return flask.render_template('index.html', data=data, version=__version__, time=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

@app.get('/')
def get_index():
	data = kiosk.data()
	return flask.render_template('matches.html', data=data, version=__version__, time=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
	
@app.get('/match/<match_id>')
def get_match(match_id):
	data = kiosk.match_data(match_id)
	if data['match']:
		return flask.render_template('match.html', data=data, version=__version__, time=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
	return flask.redirect('/', code=302)


@app.get('/json/device')
def get_json_device():
	return [kiosk.devices[id].data() for id in kiosk.devices]

@app.get('/save/device/<id>')
def save_device(id):
	if id in kiosk.devices:
		kiosk.devices[id].save()
		return {'match_def': kiosk.devices[id].match_def, 'match_scores': kiosk.devices[id].match_scores}
	else:
		return {'error', 404}, 404

@app.get('/json/match_def/<id>')
def get_json_match_def(id):
	if id in kiosk.devices:
		return kiosk.devices[id].match_def
	else:
		return {'error', 404}, 404

@app.get('/json/match_scores/<id>')
def get_json_match_scores(id):
	if id in kiosk.devices:
		return kiosk.devices[id].match_scores
	else:
		return {'error', 404}, 404

@app.get('/match2/<match_id>')
def get_match2(match_id):
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
		if data['match']['id'] == match_id:
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
				self.devices[device_name] = PSDevice(device_name, config['Devices'][device_name])
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
				match = Match.create(match_def, match_scores)
				if match:
					matches[match_id] = match
		return matches
	def match(self, match_id):
		matches = self.matches()
		return matches.get(match_id)
	def start(self):
		self.scheduler = BackgroundScheduler()
		for device_name in self.devices:
			device = self.devices[device_name]
			try:
				if device.poll_time != 0:
					self.scheduler.add_job(device.start, 'interval', seconds=device.poll_time)
				else:
					asyncio.run(device.start())
			except Exception:
				pass
		self.scheduler.start()

class Device:
	def __init__(self, id, config):
		self.id = id
		self.match_def_path = config.get('match_def_path')
		self.match_scores_path = config.get('match_scores_path')
		self.poll_time = config.get('poll_time')
		self.match_def = {}
		self.match_scores = {}
		
	def start(self):
		self.update()
	
	def update(self):
		raise NotImplementedError
	
	def save(self):
		pass
	
	def data(self):
		return {'id': self.id,
			'type': self.__class__.__name__,
			'poll_time': self.poll_time}

class JSONDevice(Device):
		
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
	
	def data(self):
		return super().data() | {'address': self.address,
			'port': self.port,
			'shutdown': self.shutdown,
			'restart': self.restart,
			'timeout': self.timeout}
	
	def __init__(self, name, config):
		super().__init__(name, config)
		self.address = config.get('address')
		self.port = config.get('port')
		self.timeout = config.get('timeout')
		self.slow_poll = config.get('slow_poll')
		self.shutdown = config.get('shutdown')
		self.restart = config.get('restart')
	
	def update(self):
		try:
			asyncio.run(asyncio.wait_for(self.temp(), timeout=self.timeout))
		except asyncio.exceptions.TimeoutError:
			print(f'{self.id}: Timeout Error')
		except OSError:
			print(f'{self.id}: OSError')
		
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
			
		if self.shutdown and self.shutdown == self.match_def.get('match_id'):
			os.system('/usr/bin/sudo /usr/sbin/shutdown -h now')
			
		if match_scores_length:
			match_scores = json.loads(zlib.decompress(await reader.readexactly(match_scores_length)))
			if match_scores:
				self.match_scores = match_scores
				
		writer.close()
		await writer.wait_closed()
	
	def save(self):
		if self.match_def and self.match_def_path:
			with open(self.match_def_path, 'w') as f:
				f.write(json.dumps(self.match_def))
		if self.match_scores and self.match_scores_path:
			with open(self.match_scores_path, 'w') as f:
				f.write(json.dumps(self.match_scores))
	
	class PSInvalidHeader(Exception):
		pass

class Match:
	_subclasses = {}
	
	@classmethod
	def register(cls, sub_type):
		def decorator(subclass):
			cls._subclasses[sub_type] = subclass
			return subclass
		return decorator
	
	@classmethod
	def create(cls, match_def, match_scores):
		sub_type = match_def.get('match_subtype')
		if sub_type not in cls._subclasses:
			return None
		return cls._subclasses[sub_type](match_def, match_scores)
	
	def __init__(self, match_def, match_scores):
		self.id = match_def.get('match_id')
		self.sub_type = match_def.get('match_subtype')
		self.shooters = {}
		self.stages = {}
		self.scores = {}
		self.update_match_data(match_def)
		self.update(match_def, match_scores)
	
	def update(self, match_def, match_scores):
		modified_date = match_def['match_modifieddate']
		if modified_date != self.modified_date:
			try:
				modified_time_1 = datetime.datetime.strptime(modified_date,'%Y-%m-%d %H:%M:%S.%f')
			except:
				modified_time_1 = datetime.datetime.strptime(modified_date,'%Y-%m-%d %H:%M:%S')
			try:
				modified_time_2 = datetime.datetime.strptime(self.modified_date,'%Y-%m-%d %H:%M:%S.%f')
			except:
				modified_time_2 = datetime.datetime.strptime(self.modified_date,'%Y-%m-%d %H:%M:%S')
			if modified_time_1 > modified_time_2:
				self.update_match_data(match_def)
		if 'match_shooters' in match_def:
			self.update_shooters(match_def['match_shooters'])
		if 'match_stages' in match_def:
			self.update_stages(match_def['match_stages'])
		if 'match_scores' in match_scores:
			self.update_scores(match_scores['match_scores'])
		#self.name = match_def.get('match_name', '')
		
	def update_match_data(self, match_def):
		self.name = match_def.get('match_name')
		self.modified_date = match_def.get('match_modifieddate')
		
	def update_scores(self, match_scores):
		for stage in match_scores:
			for stage_stagescore in stage['stage_stagescores']:
				shooter_id = stage_stagescore['shtr']
				stage_id = stage['stage_uuid']
				if stage_id not in self.scores:
					self.scores[stage_id] = {}
				if shooter_id in self.scores[stage_id]:
					self.scores[stage_id][shooter_id].update_if_modified(stage_stagescore)
				else:
					score = StageScore.create(self, stage_id, stage_stagescore)
					if score:
						self.scores[stage_id][shooter_id] = score
	
	def update_stage(self, match_stage):
		stage_id = match_stage['stage_uuid']
		if stage_id in self.stages:
			self.stages[stage_id].update_if_modified(match_stage)
		else:
			stage = Stage.create(self, match_stage)
			if stage:
				self.stages[stage_id] = stage
	
	def update_stages(self, match_stages):
		for match_stage in match_stages:
			self.update_stage(match_stage)
	
	def update_shooter(self, match_shooter):
		shooter_id = match_shooter['sh_uid']
		if shooter_id in self.shooters:
			self.shooters[shooter_id].update_if_modified(match_shooter)
		else:
			shooter = Shooter.create(self, match_shooter)
			if shooter:
				self.shooters[shooter_id] = shooter
	
	def update_shooters(self, match_shooters):
		for match_shooter in match_shooters:
			self.update_shooter(match_shooter)
	#def shooter_list(self):
	#	return [id for id in self.shooters if not self.shooters[id].disqualified and not self.shooters[id].deleted]
	#def stage_list(self):
	#	return [id for id in self.stages if not self.stages[id].deleted]
	def shooter_list_by_division(self):
		shooter_list = self.shooter_list()
		divisions = {self.shooters[id].division for id in shooter_list}
		return {division: [id for id in shooter_list if self.shooters[id].division == division] for division in divisions}
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
	#def stage_data(self):
	#	stage_list = self.stage_list()
	#	for id in stage_list:
	#		self.stages[id].post_process()
	#	return [self.stages[id].data() for id in stage_list]
	def stage(self, stage_id):
		return self.stages.get(stage_id)
	def score_data(self):
		return [[self.scores[stage_id][shooter_id].data() for stage_id in self.scores for shooter_id in self.scores[stage_id]]]
	def data(self):
		self.post_process()
		return {'name': self.name, 'id': self.id, 'stages': self.stage_data, 'scores': self.score_data(), 'sub_type': self.sub_type}
	def post_process(self):
		self.stage_list = [id for id in self.stages if not self.stages[id].deleted]
		self.shooter_list = [id for id in self.shooters if not self.shooters[id].disqualified and not self.shooters[id].deleted]
		self.divisions = {self.shooters[id].division for id in self.shooter_list}
		self.shooter_list_by_division = {division: [id for id in self.shooter_list if self.shooters[id].division == division] for division in self.divisions}
		for stage_id in self.scores:
			for shooter_id in self.scores[stage_id]:
				self.scores[stage_id][shooter_id].post_process()
		for id in self.stage_list:
			self.stages[id].post_process()
		for id in self.shooter_list:
			self.shooters[id].post_process()
		self.stage_data = [self.stages[id].data() for id in self.stage_list]
		
@Match.register('ipsc')
class IPSCMatch(Match):
	def update_match_data(self, match_def):
		super().update_match_data(match_def)
		self.match_pfs = {pf['name'].lower():pf for pf in match_def.get('match_pfs')}
	def data(self):
		return super().data() | {'match_pfs': self.match_pfs,
			'stages':self.stage_data(),
			'divisions':self.shooter_by_division()}

@Match.register('silhouette')
class SilhouetteMatch(Match):
	def data(self):
		return super().data() | {'divisions': self.shooter_by_division()}

@Match.register('scsa')
class SCSAMatch(Match):
	def data(self):
		return super().data() | {'divisions': self.shooter_by_division()}

@Match.register('sass')
class SASSMatch(Match):
	def data(self):
		return super().data() | {'divisions': self.shooter_by_division()}

class Shooter:
	_subclasses = {}
	
	@classmethod
	def register(cls, sub_type):
		def decorator(subclass):
			cls._subclasses[sub_type] = subclass
			return subclass
		return decorator
	
	@classmethod
	def create(cls, match, match_shooter):
		sub_type = match.sub_type
		if sub_type not in cls._subclasses:
			return None
		return cls._subclasses[sub_type](match, match_shooter)
	
	def __init__(self, match, match_shooter):
		self.id = match_shooter['sh_uid']
		self.match = match
		self.update(match_shooter)
	
	def update_if_modified(self, match_shooter):
		modified_date = match_shooter['sh_mod']
		if match_shooter['sh_mod'] != self.modified_date:
			try:
				modified_time_1 = datetime.datetime.strptime(modified_date,'%Y-%m-%d %H:%M:%S.%f')
			except:
				modified_time_1 = datetime.datetime.strptime(modified_date,'%Y-%m-%d %H:%M:%S')
			try:
				modified_time_2 = datetime.datetime.strptime(self.modified_date,'%Y-%m-%d %H:%M:%S.%f')
			except:
				modified_time_2 = datetime.datetime.strptime(self.modified_date,'%Y-%m-%d %H:%M:%S')
			if modified_time_1 > modified_time_2:
				self.update(match_shooter)
	
	def update(self, match_shooter):
		self.firstname = match_shooter.get('sh_fn', '')
		self.lastname = match_shooter.get('sh_ln', '')
		self.division = match_shooter.get('sh_dvp', '')
		if self.division in kiosk.division_name_substitutions:
			self.short_division = kiosk.division_name_substitutions[self.division]
		else:
			self.short_division = self.division
		self.deleted = match_shooter.get('sh_del', False)
		self.disqualified = match_shooter.get('sh_dq', False)
		self.modified_date = match_shooter['sh_mod']
	
	def name(self):
		return f'{self.firstname} {self.lastname}'
		
	def data(self):
		return {'name': self.name(),
			'short_division': self.short_division}
	def post_process(self):
		pass

@Shooter.register('ipsc')
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
			for stage_id in [id for id in self.match.stages if not self.match.stages[id].deleted]:
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
		return super().data() | {'hits': self.hits,
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

@Shooter.register('silhouette')
class SilhouetteShooter(Shooter):
	def data(self):
		return super().data() | {'targets': self.targets, 'match_points_total': self.match_points_total, 'match_points': self.match_points}
	
	def post_process(self):
		stage_list = self.match.stage_list
		scores = self.match.scores
		self.targets = {stage_id: scores[stage_id][self.id].targets if stage_id in scores and self.id in scores[stage_id] else self.match.stages[stage_id].blank_score for stage_id in stage_list}
		self.match_points = {stage_id: scores[stage_id][self.id].score if stage_id in scores and self.id in scores[stage_id] else 0 for stage_id in stage_list}
		self.match_points_total = sum(self.match_points[stage_id] for stage_id in self.match_points)

@Shooter.register('scsa')
class SCSAShooter(Shooter):
	def data(self):
		return super().data() | {'scores': self.scores, 'time':self.time, 'scores_string': self.scores_string, 'time_string':self.time_string}
	
	def post_process(self):
		stage_list = self.match.stage_list
		scores = self.match.scores
		self.scores = {stage_id: scores[stage_id][self.id].score if stage_id in scores and self.id in scores[stage_id] else 120 for stage_id in stage_list}
		self.scores_string = {stage_id: '-' if self.scores[stage_id] == 120 else f'{self.scores[stage_id]:.2f}' for stage_id in self.scores}
		self.time = sum(self.scores[stage_id] for stage_id in self.scores)
		self.time_string = '-' if self.time == 480 else f'{self.time:.2f}'
		#self.targets = {stage_id: scores[stage_id][self.id].targets if stage_id in scores and self.id in scores[stage_id] else self.match.stages[stage_id].blank_score for stage_id in stage_list}
		#self.match_points = {stage_id: scores[stage_id][self.id].score if stage_id in scores and self.id in scores[stage_id] else 0 for stage_id in stage_list}
		#self.match_points_total = sum(self.match_points[stage_id] for stage_id in self.match_points)

@Shooter.register('sass')
class SASSShooter(Shooter):
	def update(self, match_shooter):
		super().update(match_shooter)
		self.alias = match_shooter.get('sh_al', '')
	def data(self):
		return super().data() | {'scores': self.scores, 'time':self.time, 'scores_string': self.scores_string, 'time_string':self.time_string, 'miss_string':self.miss_string, 'penalties_string':self.penalties_string, 'string_string':self.string_string}
	def name(self):
		return self.alias
	def post_process(self):
		stage_list = self.match.stage_list
		scores = self.match.scores
		self.scores = {stage_id: scores[stage_id][self.id].score if stage_id in scores and self.id in scores[stage_id] else 300 for stage_id in stage_list}
		self.string = {stage_id: scores[stage_id][self.id].string if stage_id in scores and self.id in scores[stage_id] else 0 for stage_id in stage_list}
		self.penalties = {stage_id: scores[stage_id][self.id].penalties if stage_id in scores and self.id in scores[stage_id] else 300 for stage_id in stage_list}
		self.miss = {stage_id: scores[stage_id][self.id].miss if stage_id in scores and self.id in scores[stage_id] else 0 for stage_id in stage_list}
		self.scores_string = {stage_id: '-' if self.scores[stage_id] == 300 else f'{self.scores[stage_id]:.2f}' for stage_id in self.scores}
		self.string_string = {stage_id: '' if self.string[stage_id] == 0 else f'{self.string[stage_id]:.2f}' for stage_id in self.string}
		self.penalties_string = {stage_id: '' if self.penalties[stage_id] == 0 else f'{self.penalties[stage_id]:.2f}' for stage_id in self.penalties}
		self.miss_string = {stage_id: '' if self.miss[stage_id] == 0 else f'{self.miss[stage_id]}' for stage_id in self.miss}
		self.time = sum(self.scores[stage_id] for stage_id in self.scores)
		self.time_string = '-' if self.time == 300*len(self.scores) else f'{self.time:.2f}'

class Stage:
	_subclasses = {}
	
	@classmethod
	def register(cls, sub_type):
		def decorator(subclass):
			cls._subclasses[sub_type] = subclass
			return subclass
		return decorator
	
	@classmethod
	def create(cls, match, match_stage):
		sub_type = match.sub_type
		if sub_type not in cls._subclasses:
			return None
		return cls._subclasses[sub_type](match, match_stage)

	def __init__(self, match, match_stage):
		self.match = match
		self.id = match_stage['stage_uuid']
		self.update(match_stage)
		
	def update_if_modified(self, match_stage):
		modified_date = match_stage['stage_modifieddate']
		if modified_date != self.modified_date:
			modified_date_1 = datetime.datetime.strptime(modified_date,'%Y-%m-%d %H:%M:%S.%f')
			modified_date_2 = datetime.datetime.strptime(self.modified_date,'%Y-%m-%d %H:%M:%S.%f')
			if modified_date_1 > modified_date_2:
				self.update(match_stage)
	
	def update(self, match_stage):
		self.number = match_stage.get('stage_number')
		self.name = match_stage.get('stage_name')
		if self.name in kiosk.stage_name_substitutions:
			self.short_name = kiosk.stage_name_substitutions[self.name]
		else:
			self.short_name = self.name
		self.modified_date = match_stage.get('stage_modifieddate')
		self.deleted = match_stage.get('stage_deleted', False)
	
	def post_process(self):
		pass
	
	def data(self):
		return {'id': self.id, 'number': self.number, 'name': self.name, 'short_name': self.short_name}

@Stage.register('ipsc')
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
		return super().data() | {'max_points': self.max_points, 'stage_reqshots': self.stage_reqshots, 'stage_poppers': self.stage_poppers, 'stage_targets': self.stage_targets, 'stage_deleted': self.deleted, 'max_hit_factor': self.max_hit_factor, 'max_hit_factors': self.max_hit_factors}

@Stage.register('silhouette')
class SilhouetteStage(Stage):
	def update(self, match_stage):
		super().update(match_stage)
		stage_customtargets = match_stage.get('stage_customtargets')
		#self.targets = [[float(target_desc[1]) for target_desc in target['target_targdesc']] for target in stage_customtargets]
		self.targets = [ [{'name': target_desc[0], 'value': target_desc[1]} for target_desc in target['target_targdesc']] for target in stage_customtargets ]
		self.blank_score = [[0 for target_desc in target['target_targdesc']] for target in stage_customtargets]
	
	def data(self):
		return super().data() | {'targets': self.targets}

@Stage.register('scsa')
class SCSAStage(Stage):
	pass

@Stage.register('sass')
class SASSStage(Stage):
	pass

class StageScore:
	_subclasses = {}
	
	@classmethod
	def register(cls, sub_type):
		def decorator(subclass):
			cls._subclasses[sub_type] = subclass
			return subclass
		return decorator
	
	@classmethod
	def create(cls, match, stage_id, stage_stagescore):
		sub_type = match.sub_type
		if sub_type not in cls._subclasses:
			return None
		return cls._subclasses[sub_type](match, stage_id, stage_stagescore)

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
		if modified_date != self.modified_date:
			modified_date_1 = datetime.datetime.strptime(modified_date,'%Y-%m-%d %H:%M:%S.%f')
			modified_date_2 = datetime.datetime.strptime(self.modified_date,'%Y-%m-%d %H:%M:%S.%f')
			if modified_date_1 > modified_date_2:
				self.update(stage_stagescore)
	
	def post_process(self):
		pass
	
	def data(self):
		return {'stage_id': self.stage_id, 'shooter_id': self.shooter_id, 'type':self.__class__.__name__} 

@StageScore.register('ipsc')
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

@StageScore.register('silhouette')
class SilhouetteStageScore(StageScore):
	def __init__(self, match, stage_id, stage_stagescore):
		super().__init__(match, stage_id, stage_stagescore)
		self.score = 0
	
	def data(self):
		return super().data() | {'score': self.score, 'targets': self.targets}
		
	def update(self, stage_stagescore):
		super().update(stage_stagescore)
		self.targets = stage_stagescore.get('cts')
		
	def post_process(self):
		if self.stage_id in self.match.stages:
			self.score = sum([ sum([int(d['value'])*c for c,d in zip(target, stage_target)]) for target, stage_target in zip(self.targets, self.match.stages[self.stage_id].targets)])
			
			#self.score = 0#numpy.sum(numpy.multiply(self.targets, self.match.stages[self.stage_id].targets))
		else:
			self.score = 0

@StageScore.register('scsa')
class SCSAStageScore(StageScore):
	def __init__(self, match, stage_id, stage_stagescore):
		super().__init__(match, stage_id, stage_stagescore)
		self.score = 0
	
	def update(self, stage_stagescore):
		super().update(stage_stagescore)
		self.strings = stage_stagescore.get('str', [0,0,0,0,0])
		self.penalties = stage_stagescore.get('penss', [[0,0,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,0]])
		
	def post_process(self):
		penalties = [3, 3, 30, 4]
		self.strings_with_penalties = [st+sum([p*q for p,q in zip(pen,penalties)]) for st,pen in zip(self.strings, self.penalties)]
		self.strings_with_penalties = [ 30 if string == 0 else min(string,30) for string in self.strings_with_penalties]
		if self.stage_id in self.match.stages:
			self.score = sum(self.strings_with_penalties) - max(self.strings_with_penalties)
		else:
			self.score = 120
	def data(self):
		return super().data() | {'score': self.score, 'strings': self.strings, 'penalties':self.penalties, 'strings_with_penalties':self.strings_with_penalties}

@StageScore.register('sass')
class SASSStageScore(StageScore):
	def __init__(self, match, stage_id, stage_stagescore):
		super().__init__(match, stage_id, stage_stagescore)
		self.score = 0
	
	def update(self, stage_stagescore):
		super().update(stage_stagescore)
		strings = stage_stagescore.get('str', [0])
		self.string = strings[0]
		self.pens = stage_stagescore.get('pens', [0,0,0,0])
		
	def post_process(self):
		penalties = [5, 10, 10, 30]
		#self.strings_with_penalties = [st+sum([p*q for p,q in zip(pen,penalties)]) for st,pen in zip(self.strings, self.penalties)]
		#self.strings_with_penalties = [ 30 if string == 0 else min(string,30) for string in self.strings_with_penalties]
		self.penalties = sum([p*q for p,q in zip(self.pens, penalties)])
		self.miss = self.pens[0]
		self.string_with_penalties = self.string + self.penalties
		self.string_with_penalties = 300 if self.string_with_penalties == 0 else min(self.string_with_penalties,300)
		if self.stage_id in self.match.stages:
			self.score = self.string_with_penalties
		else:
			self.score = 300
	def data(self):
		return super().data() | {'score': self.score, 'string': self.string, 'penalties':self.penalties, 'miss':self.miss}

if __name__ == '__main__':
	kiosk = Kiosk()
	kiosk.start()
	app.run(host='0.0.0.0', debug=True)
