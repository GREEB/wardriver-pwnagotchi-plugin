import logging
import re
import sqlite3
import os
from datetime import datetime, timezone
import toml
from threading import Lock
import json
import requests
from PIL import Image, ImageOps
import pwnagotchi.plugins as plugins
from pwnagotchi.ui.components import LabeledValue, Widget
from pwnagotchi.ui.view import BLACK
import pwnagotchi.ui.fonts as fonts
from flask import abort
from flask import render_template_string
import socket
import time

try:
    import websockets
    import asyncio
except:
    pass

class Database():
    def __init__(self, path):
        self.__path = path
        self.__db_connect()
        self.remove_empty_sessions() # Remove old sessions that don't have networks
    
    def __db_connect(self):
        logging.info('[WARDRIVER] Setting up database connection...')
        self.__connection = sqlite3.connect(self.__path, check_same_thread = False, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
        cursor = self.__connection.cursor()
        cursor.execute('CREATE TABLE IF NOT EXISTS sessions ("id" INTEGER, "created_at" TEXT DEFAULT CURRENT_TIMESTAMP, "wigle_uploaded" INTEGER DEFAULT 0, PRIMARY KEY("id" AUTOINCREMENT))') # sessions table contains wardriving sessions
        cursor.execute('CREATE TABLE IF NOT EXISTS networks ("id" INTEGER, "mac" TEXT NOT NULL, "ssid" TEXT, PRIMARY KEY ("id" AUTOINCREMENT))') # networks table contains seen networks without coordinates/sessions info
        cursor.execute('CREATE TABLE IF NOT EXISTS wardrive ("id" INTEGER, "session_id" INTEGER NOT NULL, "network_id" INTEGER NOT NULL, "auth_mode" TEXT NOT NULL, "latitude" TEXT NOT NULL, "longitude" TEXT NOT NULL, "altitude" TEXT NOT NULL, "accuracy" INTEGER NOT NULL, "channel" INTEGER NOT NULL, "rssi" INTEGER NOT NULL, "seen_timestamp" TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY("id" AUTOINCREMENT), FOREIGN KEY("session_id") REFERENCES sessions("id"), FOREIGN KEY("network_id") REFERENCES networks("id"))') # wardrive table contains the relations between sessions and networks with timestamp and coordinates
        cursor.close()
        self.__connection.commit()
        logging.info('[WARDRIVER] Succesfully connected to db')
    
    def disconnect(self):
        self.__connection.commit()
        self.__connection.close()
        logging.info('[WARDRIVER] Closed db connection')

    def new_wardriving_session(self, timestamp = None, wigle_uploaded = False):
        cursor = self.__connection.cursor()
        if timestamp:
            cursor.execute('INSERT INTO sessions(created_at, wigle_uploaded) VALUES (?, ?)', [timestamp, wigle_uploaded])
        else:
            cursor.execute('INSERT INTO sessions(wigle_uploaded) VALUES (?)', [wigle_uploaded]) # using default values
        session_id = cursor.lastrowid
        cursor.close()
        self.__connection.commit()
        return session_id
    
    def add_wardrived_network(self, session_id, mac, ssid, auth_mode, latitude, longitude, altitude, accuracy, channel, rssi, seen_timestamp = None):
        cursor = self.__connection.cursor()
        cursor.execute('SELECT id FROM networks WHERE mac = ? AND ssid = ?', [mac, ssid])
        network = cursor.fetchone()
        network_id = network[0] if network else None
        if(not network_id):
            cursor.execute('INSERT INTO networks(mac, ssid) VALUES (?, ?)', [mac, ssid])
            network_id = cursor.lastrowid
        
        if seen_timestamp:
            cursor.execute('INSERT INTO wardrive(session_id, network_id, auth_mode, latitude, longitude, altitude, accuracy, channel, rssi, seen_timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', [session_id, network_id, auth_mode, latitude, longitude, altitude, accuracy, channel, rssi, seen_timestamp])
        else:
            cursor.execute('INSERT INTO wardrive(session_id, network_id, auth_mode, latitude, longitude, altitude, accuracy, channel, rssi) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', [session_id, network_id, auth_mode, latitude, longitude, altitude, accuracy, channel, rssi])
        cursor.close()
        self.__connection.commit()
   
    def session_networks_count(self, session_id):
        '''
        Return the total networks count for a wardriving session given its id
        '''
        cursor = self.__connection.cursor()
        cursor.execute('SELECT COUNT(wardrive.id) FROM wardrive JOIN networks ON wardrive.network_id = networks.id WHERE wardrive.session_id = ? GROUP BY wardrive.session_id', [session_id])
        row = cursor.fetchone()
        cursor.close()
        return row[0] if row else 0

    def session_networks(self, session_id):
        '''
        Return networks data for a wardriving session given its id
        '''
        cursor = self.__connection.cursor()
        networks = []
        cursor.execute('SELECT networks.mac, networks.ssid, wardrive.auth_mode, wardrive.latitude, wardrive.longitude, wardrive.altitude, wardrive.accuracy, wardrive.channel, wardrive.rssi, wardrive.seen_timestamp FROM wardrive JOIN networks ON wardrive.network_id = networks.id WHERE wardrive.session_id = ?', [session_id])
        rows = cursor.fetchall()
        for row in rows:
            mac, ssid, auth_mode, latitude, longitude, altitude, accuracy, channel, rssi, seen_timestamp = row
            networks.append({
                'mac': mac,
                'ssid': ssid,
                'auth_mode': auth_mode,
                'latitude': latitude,
                'longitude': longitude,
                'altitude': altitude,
                'accuracy': accuracy,
                'channel': channel,
                'rssi': rssi,
                'seen_timestamp': seen_timestamp
            })
        cursor.close()
        return networks

    def session_uploaded_to_wigle(self, session_id):
        cursor = self.__connection.cursor()
        cursor.execute('UPDATE sessions SET "wigle_uploaded" = 1 WHERE id = ?', [session_id])
        cursor.close()
        self.__connection.commit()
    
    def wigle_sessions_not_uploaded(self, current_session_id):
        '''
        Return the list of ids of sessions that haven't got uploaded on WiGLE excluding `current_session_id`
        '''
        cursor = self.__connection.cursor()
        sessions_ids = []
        cursor.execute('SELECT id FROM sessions WHERE wigle_uploaded = 0 AND id <> ?', [current_session_id])
        rows = cursor.fetchall()
        for row in rows:
            sessions_ids.append(row[0])
        cursor.close()
        return sessions_ids

    def remove_empty_sessions(self):
        '''
        Remove all sessions that doesn't have any network
        '''
        cursor = self.__connection.cursor()
        cursor.execute('DELETE FROM sessions WHERE sessions.id NOT IN (SELECT wardrive.session_id FROM wardrive GROUP BY wardrive.session_id)')
        cursor.close()
        self.__connection.commit()
    
    # Web UI queries
    def general_stats(self):
        cursor = self.__connection.cursor()
        cursor.execute('SELECT COUNT(id) FROM networks')
        total_networks = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(id) FROM sessions')
        total_sessions = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(id) FROM sessions WHERE wigle_uploaded = 1')
        sessions_uploaded = cursor.fetchone()[0]
        cursor.close()
        return {
            'total_networks': total_networks,
            'total_sessions': total_sessions,
            'sessions_uploaded': sessions_uploaded
        }
    
    def sessions(self):
        cursor = self.__connection.cursor()
        cursor.execute('SELECT sessions.*, COUNT(wardrive.id) FROM sessions JOIN wardrive ON sessions.id = wardrive.session_id GROUP BY sessions.id')
        rows = cursor.fetchall()
        sessions = []
        for row in rows:
            sessions.append({
                'id': row[0],
                'created_at': row[1],
                'wigle_uploaded': row[2] == 1,
                'networks': row[3]
            })
        cursor.close()
        return sessions
    
    def current_session_stats(self, session_id):
        cursor = self.__connection.cursor()
        cursor.execute('SELECT created_at FROM sessions WHERE id = ?', [session_id])
        created_at = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(id) FROM wardrive WHERE session_id = ?', [session_id])
        networks = cursor.fetchone()[0]
        cursor.close()
        return {
            "id": session_id,
            "created_at": created_at,
            "networks": networks
        }

    def networks(self):
        cursor = self.__connection.cursor()
        cursor.execute('SELECT n.*, MIN(w.seen_timestamp), MIN(w.session_id), MAX(w.seen_timestamp), MAX(w.session_id), COUNT(n.id) FROM networks n JOIN wardrive w ON n.id = w.network_id GROUP BY n.id')
        rows = cursor.fetchall()
        networks = []
        for row in rows:
            id, mac, ssid, first_seen, first_session, last_seen, last_session, sessions_count = row
            networks.append({
                "id": id,
                "mac": mac,
                "ssid": ssid,
                "first_seen": first_seen,
                "first_session": first_session,
                "last_seen": last_seen,
                "last_session": last_session,
                "sessions_count": sessions_count
            })
        cursor.close()
        
        return networks

    def map_networks(self):
        cursor = self.__connection.cursor()
        cursor.execute('SELECT n.mac, n.ssid, w.latitude, w.longitude, w.altitude, w.accuracy FROM networks n JOIN wardrive w ON n.id = w.network_id')
        rows = cursor.fetchall()
        networks = []
        for row in rows:
            mac, ssid, latitude, longitude, altitude, accuracy = row
            networks.append({
                "mac": mac,
                "ssid": ssid,
                "latitude": float(latitude),
                "longitude": float(longitude),
                "altitude": float(altitude),
                "accuracy": int(accuracy)
            })
        cursor.close()
        
        return networks

class CSVGenerator():
    def __init__(self):
       self.__wigle_info()
        
    def __wigle_info(self):
        '''
        Return info used in CSV pre-header
        '''
        try:
            with open('/etc/pwnagotchi/config.toml', 'r') as config_file:
                data = toml.load(config_file)
                # Pwnagotchi name
                device = data['main']['name']
                # Pwnagotchi display model
                display = data['ui']['display']['type'] # Pwnagotchi display
        except Exception:
            device = 'pwnagotchi'
            display = 'unknown'

        # Preheader formatting
        file_format = 'WigleWifi-1.4'
        app_release = Wardriver.__version__
        # Device model
        try:
            with open('/sys/firmware/devicetree/base/model', 'r') as model_info:
                model = model_info.read()
        except Exception:
            model = 'unknown'
        # OS version
        try:
            with open('/etc/os-release', 'r') as release_info:
                release = release_info.read().split('\n')[0].split('=')[-1].replace('"', '')
        except Exception:
            release = 'unknown'
        # CPU model
        try:
            with open('/proc/cpuinfo', 'r') as cpu_model:
                board = cpu_model.read().split('\n')[1].split(':')[1][1:]
        except Exception:
            board = 'unknown'
        
        # Brand: currently set equal to model
        brand = model

        self.__wigle_file_format = file_format
        self.__wigle_app_release = app_release
        self.__wigle_model = model
        self.__wigle_release = release
        self.__wigle_device = device
        self.__wigle_display = display
        self.__wigle_board = board
        self.__wigle_brand = brand

    def __csv_header(self):
        return 'MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n'
    
    def __csv_network(self, network):
        return f'{network["mac"]},{network["ssid"]},{network["auth_mode"]},{network["seen_timestamp"]},{network["channel"]},{network["rssi"]},{network["latitude"]},{network["longitude"]},{network["altitude"]},{network["accuracy"]},WIFI\n'

    def networks_to_csv(self, networks):
        csv = self.__csv_header()
        for network in networks:
            csv += self.__csv_network(network)
        return csv

    def networks_to_wigle_csv(self, networks):
        pre_header = f'{self.__wigle_file_format},{self.__wigle_app_release},{self.__wigle_model},{self.__wigle_release},{self.__wigle_device},{self.__wigle_display},{self.__wigle_board},{self.__wigle_brand}\n'
        
        return pre_header + self.networks_to_csv(networks)

# Credits to Rai68: https://github.com/rai68/gpsd-easy
class GpsdClient():
    DEFAULT_HOST = '127.0.0.1'
    DEFAULT_PORT = 2947
    MAX_RETRIES = 5

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.__gpsd_socket = None
        self.__gpsd_stream = None
    
    def connect(self):
        logging.debug('[WARDRIVER] Connecting to GPSD socket')
        for attempt in range(self.MAX_RETRIES):
            try:
                self.__gpsd_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.__gpsd_socket.connect((self.host, self.port))
                self.__gpsd_stream = self.__gpsd_socket.makefile(mode="rw")
                self.__gpsd_stream.write('?WATCH={"enable":true}\n')
                self.__gpsd_stream.flush()

                response_raw = self.__gpsd_stream.readline()
                response = json.loads(response_raw)
                if response['class'] != 'VERSION':
                    raise Exception('Invalid response received from GPSD socket')
                logging.info('[WARDRIVER] Connected to GPSD socket')
                return
            except Exception as e:
                logging.debug(f'[WARDRIVER] Failed connecting to GPSD socket (attempt {attempt + 1}/{self.MAX_RETRIES}): {e}')
                time.sleep(5) # Sleep 5s between each try

    def disconnect(self):
        if self.__gpsd_socket:
            self.__gpsd_socket.close()
            self.__gpsd_socket = None
            self.__gpsd_stream = None

    def get_coordinates(self):
        for attempt in range(self.MAX_RETRIES):
            try:
                self.__gpsd_stream.write('?POLL;\n')
                self.__gpsd_stream.flush()

                response_raw = self.__gpsd_stream.readline().strip()
                if response_raw is None or response_raw == '':
                    continue

                response = json.loads(response_raw)

                if 'class' in response and response['class'] == 'POLL' and 'tpv' in response and len(response['tpv']) > 0:
                    return {
                        'Latitude': response['tpv'][0].get('lat', None),
                        'Longitude': response['tpv'][0].get('lon', None),
                        'Altitude': response['tpv'][0].get('alt', None)
                    }
            except:
                logging.error('[WARDRIVER] GPSD socket error. Reconnecting...')
                self.disconnect()
                try:
                    self.connect()
                except:
                    return None
        return None

# Credits to Jayofelony: https://github.com/jayofelony/pwnagotchi-torch-plugins/blob/main/pwndroid.py
class PwndroidClient:
    DEFAULT_HOST = '192.168.44.1'
    DEFAULT_PORT = 8080

    def __init__(self, host='192.168.44.1', port=8080):
        self.host = host
        self.port = port
        self.coordinates = {
            'Latitude': None,
            'Longitude': None,
            'Altitude': None
        }
        self.__destroy = False
        self.__websocket = None
    
    async def connect(self):
        while not self.__websocket and not self.__destroy:
            try:
                self.__websocket = await websockets.connect(f'ws://{self.host}:{self.port}')
                logging.info('[WARDRIVER] Connected to pwndroid websocket')
                await self.__get_gps_coordinates()
            except Exception as e:
                logging.critical('[WARDRIVER] Failed to connect to pwndroid websocket')
                self.__websocket = None
                await asyncio.sleep(10) # Wait 10 seconds between each retry
    
    async def disconnect(self):
        if self.__websocket:
            await self.__websocket.close()
            logging.info('[WARDRIVER] Closed connection to pwndroid websocket')
            self.__websocket = None
            self.__destroy = True
        else:
            logging.debug('[WARDRIVER] Cannot close websocket connection. No connection estabilished')
    
    def is_connected(self):
        return self.__websocket is not None
    
    async def __get_gps_coordinates(self):
        while self.__websocket:
            try:
                message = await self.__websocket.recv()
                data = json.loads(message)

                if 'Latitude' in data and 'Longitude' in data and 'Altitude' in data:
                    self.coordinates['Latitude'] = data['Latitude']
                    self.coordinates['Longitude'] = data['Longitude']
                    self.coordinates['Altitude'] = data['Altitude']
                else:
                    logging.debug(f'[WARDRIVER] Invalid GPS data received from websocket: {json.dumps(data)}')
                await asyncio.sleep(5) # Sleep for 5 seconds
            except websockets.exceptions.ConnectionClosed:
                logging.critical('[WARDRIVER] Websocket connection closed by pwndroid application. Will try to restabilish connection')
                self.__websocket = None
            except json.JSONDecodeError:
                logging.debug('[WARDRIVER] Invalid data. Cannot decode as JSON data')
            except Exception as e:
                logging.error(f'[WARDRIVER] Error while getting GPS position. {e}')


class Wardriver(plugins.Plugin):
    __author__ = 'CyberArtemio'
    __version__ = '2.3'
    __license__ = 'GPL3'
    __description__ = 'A wardriving plugin for pwnagotchi. Saves all networks seen and uploads data to WiGLE once internet is available'

    DEFAULT_PATH = '/root/wardriver' # SQLite database default path
    DATABASE_NAME = 'wardriver.db' # SQLite database file name
    ASSETS_URL = [
        {
            "name": "icon_error.bmp",
            "url": "https://raw.githubusercontent.com/cyberartemio/wardriver-pwnagotchi-plugin/refs/heads/main/wardriver_assets/icon_error.bmp"
        },
        {
            "name": "icon_working.bmp",
            "url": "https://raw.githubusercontent.com/cyberartemio/wardriver-pwnagotchi-plugin/refs/heads/main/wardriver_assets/icon_working.bmp"
        }
    ]

    def __init__(self):
        logging.debug('[WARDRIVER] Plugin created')
        self.__db = None
        self.__current_icon = ""
        self.ready = False
        self.__downloaded_assets = True
        self.__agent_mode = None
        self.__last_gps = {
            "latitude": '-',
            "longitude": '-',
            "altitude": '-'
        }
    
    def on_loaded(self):
        logging.info('[WARDRIVER] Plugin loaded (join the Discord server: https://discord.gg/5vrJbbW3ve)')

        self.__lock = Lock()
        self.__gps_available = True

        try:
            self.__path = self.options['path']
        except Exception:
            self.__path = self.DEFAULT_PATH
        
        try:
            self.__ui_enabled = self.options['ui']['enabled']
        except Exception:
            self.__ui_enabled = False
        
        try:
            self.__icon = self.options['ui']['icon']
        except Exception:
            self.__icon = True
        
        self.__assets_path = os.path.join(os.path.dirname(__file__), "wardriver_assets")
        os.makedirs(self.__assets_path, exist_ok=True)
        for asset in self.ASSETS_URL:
            if not os.path.isfile(os.path.join(self.__assets_path, asset["name"])):
                logging.critical(f'[WARDRIVER] Asset {asset["name"]} is missing. Once internet is available it will be downloaded from GitHub')
                self.__downloaded_assets = False
                self.__icon = False
        
        try:
            self.__reverse = self.options['ui']['icon_reverse']
        except Exception:
            self.__reverse = False

        try:
            self.__ui_position = (self.options['ui']['position']['x'], self.options['ui']['position']['y'])
        except Exception:
            self.__ui_position = (7, 95)
        
        try:
            self.__whitelist = self.options['whitelist']
        except Exception:
            self.__whitelist = []

        try:
            self.__wigle_api_key = self.options['wigle']['api_key']
        except Exception:
            self.__wigle_api_key = None
        try:
            self.__wigle_donate = self.options['wigle']['donate']
        except Exception:
            self.__wigle_donate = False
        try:
            self.__wigle_enabled = self.options['wigle']['enabled']
            
            if self.__wigle_enabled and (not self.__wigle_api_key or self.__wigle_api_key == ''):
                logging.error('[WARDRIVER] Wigle enabled but no api key provided!')
                self.__wigle_enabled = False
        except Exception:
            self.__wigle_enabled = False
        
        self.__gps_config = dict()
        try:
            self.__gps_config['method'] = self.options['gps']['method']
            if self.__gps_config['method'] not in ['bettercap', 'gpsd', 'pwndroid']:
                logging.critical('[WARDRIVER] Invalid GPS method provided! Switching back to bettercap (default)')
                raise Error()
        except:
            self.__gps_config['method'] = 'bettercap'
        
        if not os.path.exists(self.__path):
            os.makedirs(self.__path)
            logging.warning('[WARDRIVER] Created db directory')
        
        self.__db = Database(os.path.join(self.__path, self.DATABASE_NAME))
        self.__csv_generator = CSVGenerator()
        self.__session_reported = []
        self.__last_ap_refresh = None
        self.__last_ap_reported = []

        logging.info(f'[WARDRIVER] Wardriver DB can be found in {self.__path}')
        
        self.__load_global_whitelist()
        if len(self.__whitelist) > 0:
            logging.info(f'[WARDRIVER] Ignoring {len(self.__whitelist)} networks')
        
        if self.__wigle_enabled:
            logging.info('[WARDRIVER] Previous sessions will be uploaded to WiGLE once internet is available')
            logging.info('[WARDRIVER] Join the WiGLE group: search "The crew of the Black Pearl" and start wardriving with us!')

        self.__session_id = self.__db.new_wardriving_session()

        self.ready = True

        if self.__gps_config['method'] == 'gpsd':
            try:
                self.__gps_config['host'] = self.options['gps']['host']
                self.__gps_config['port'] = self.options['gps']['port']
            except:
                self.__gps_config['host'] = GpsdClient.DEFAULT_HOST
                self.__gps_config['port'] = GpsdClient.DEFAULT_PORT

            try:
                self.__gpsd_client = GpsdClient(host=self.__gps_config['host'], port=self.__gps_config['port'])
                self.__gpsd_client.connect()
            except:
                logging.critical('[WARDRIVER] Failed connecting to GPSD. Will try again soon.')
        elif self.__gps_config['method'] == 'pwndroid':
            try:
                self.__gps_config['host'] = self.options['gps']['host']
                self.__gps_config['port'] = self.options['gps']['port']
            except:
                self.__gps_config['host'] = PwndroidClient.DEFAULT_HOST
                self.__gps_config['port'] = PwndroidClient.DEFAULT_PORT
            try:
                self.__pwndroid_client = PwndroidClient(self.__gps_config['host'], self.__gps_config['port'])
                asyncio.run(self.__pwndroid_client.connect())
            except Exception as e:
                logging.critical(f'[WARDRIVER] Unexpected error while connecting to pwndroid. Error: {e}')
    
    def on_ready(self, agent):
        self.__agent_mode = agent.mode
        
    def __load_global_whitelist(self):
        try:
            with open('/etc/pwnagotchi/config.toml', 'r') as config_file:
                data = toml.load(config_file)
                for ssid in data['main']['whitelist']:
                    if ssid not in self.__whitelist:
                        self.__whitelist.append(ssid)
        except Exception as e:
            logging.critical('[WARDRIVER] Cannot read global config. Networks in global whitelist will NOT be ignored')
    
    def on_ui_setup(self, ui):
        if self.__ui_enabled:
            logging.info('[WARDRIVER] Adding status text to ui')
            wardriver_text_pos = (self.__ui_position[0] + 13, self.__ui_position[1]) if self.__icon else self.__ui_position
            wardriver_text_label = '' if self.__icon else 'wardrive:'
            ui.add_element('wardriver', LabeledValue(color = BLACK,
                                            label = wardriver_text_label,
                                            value = "Not started",
                                            position = wardriver_text_pos,
                                            label_font = fonts.Small,
                                            text_font = fonts.Small))
            
            if self.__icon:
                ui.add_element('wardriver_icon', WardriverIcon(path = f'{self.__assets_path}/icon_working.bmp', xy = self.__ui_position, reverse = self.__reverse))
                self.__current_icon = 'icon_working'

    def on_ui_update(self, ui):
        if self.__gps_config['method'] == 'gpsd' and self.ready:
            self.__gpsd_client.get_coordinates() # Poll to keep the socket open
        if self.__ui_enabled and self.ready and self.__agent_mode and self.__agent_mode != "manual":
            ui.set('wardriver', f'{self.__db.session_networks_count(self.__session_id)} {"networks" if self.__icon else "nets"}')
            if self.__gps_available and self.__current_icon == 'icon_error':
                ui.remove_element('wardriver_icon')
                ui.add_element('wardriver_icon', WardriverIcon(path = f'{self.__assets_path}/icon_working.bmp', xy = self.__ui_position, reverse = self.__reverse))
                self.__current_icon = 'icon_working'
            elif not self.__gps_available and self.__current_icon == 'icon_working':
                ui.remove_element('wardriver_icon')
                ui.add_element('wardriver_icon', WardriverIcon(path = f'{self.__assets_path}/icon_error.bmp', xy = self.__ui_position, reverse = self.__reverse))
                self.__current_icon = 'icon_error'

    def on_unload(self, ui):
        if self.__ui_enabled:
            with ui._lock:
                ui.remove_element('wardriver')
                if self.__icon:
                    ui.remove_element('wardriver_icon')
        if self.__gps_config['method'] == 'gpsd':
            self.__gpsd_client.disconnect()
        if self.__gps_config['method'] == 'pwndroid':
            asyncio.run(self.__pwndroid_client.disconnect())
        self.__db.disconnect()
        logging.info('[WARDRIVER] Plugin unloaded')

    def __filter_whitelist_aps(self, unfiltered_aps):
        '''
        Filter whitelisted networks
        '''
        filtered_aps = [ ap for ap in unfiltered_aps if ap['hostname'] not in self.__whitelist ]
        return filtered_aps
    
    def __filter_reported_aps(self, unfiltered_aps):
        '''
        Filter already reported networks
        '''
        filtered_aps = [ ap for ap in unfiltered_aps if (ap['mac'], ap['hostname']) not in self.__session_reported ]
        return filtered_aps

    def on_unfiltered_ap_list(self, agent, aps):
        gps_data = None
        if not self.ready: # it is ready once the session file has been initialized with pre-header and header
            logging.error('[WARDRIVER] Plugin not ready... skip wardriving log')
            return
        
        if self.__gps_config['method'] == 'bettercap':
            info = agent.session()
            gps_data = info["gps"]

        if self.__gps_config['method'] == 'gpsd':
            try:
                gps_data = self.__gpsd_client.get_coordinates()
            except:
               gps_data = None
        
        if self.__gps_config['method'] == 'pwndroid':
            if self.__pwndroid_client.is_connected():
                gps_data = self.__pwndroid_client.coordinates

        if gps_data and all([ gps_data["Latitude"], gps_data["Longitude"] ]):
            self.__gps_available = True
            self.__last_ap_refresh = datetime.now()
            self.__last_ap_reported = []
            coordinates = {
                'latitude': gps_data["Latitude"],
                'longitude': gps_data["Longitude"],
                'altitude': gps_data["Altitude"],
                'accuracy': 50 # TODO: how can this be calculated?
            }

            self.__last_gps['latitude'] = gps_data['Latitude']
            self.__last_gps['longitude'] = gps_data['Longitude']
            self.__last_gps['altitude'] = gps_data['Altitude']

            filtered_aps = self.__filter_whitelist_aps(aps)
            filtered_aps = self.__filter_reported_aps(filtered_aps)
            
            if len(filtered_aps) > 0:
                logging.info(f'[WARDRIVER] Discovered {len(filtered_aps)} new networks')
                for ap in filtered_aps:
                    mac = ap['mac']
                    ssid = ap['hostname'] if ap['hostname'] != '<hidden>' else ''
                    capabilities = ''
                    if ap['encryption'] != '':
                        capabilities = f'{capabilities}[{ap["encryption"]}]'
                    if ap['cipher'] != '':
                        capabilities = f'{capabilities}[{ap["cipher"]}]'
                    if ap['authentication'] != '':
                        capabilities = f'{capabilities}[{ap["authentication"]}]'
                    channel = ap['channel']
                    rssi = ap['rssi']
                    self.__last_ap_reported.append({
                        "mac": mac,
                        "ssid": ssid,
                        "capabilities": capabilities,
                        "channel": channel,
                        "rssi": rssi
                    })
                    self.__session_reported.append((mac, ssid))
                    self.__db.add_wardrived_network(session_id = self.__session_id,
                                                    mac = mac,
                                                    ssid = ssid,
                                                    auth_mode = capabilities,
                                                    channel = channel,
                                                    rssi = rssi,
                                                    latitude = coordinates['latitude'],
                                                    longitude = coordinates['longitude'],
                                                    altitude = coordinates['altitude'],
                                                    accuracy = coordinates['accuracy'])
        else:
            self.__gps_available = False
            self.__last_gps['latitude'] = '-'
            self.__last_gps['longitude'] = '-'
            self.__last_gps['altitude'] = '-'
            logging.warning("[WARDRIVER] GPS not available... skip wardriving log")
        
    def __upload_session_to_wigle(self, session_id):
        if self.__wigle_api_key != '':
            headers = {
                'Authorization': f'Basic {self.__wigle_api_key}',
                'Accept': 'application/json'
            }
            networks = self.__db.session_networks(session_id)
            csv = self.__csv_generator.networks_to_wigle_csv(networks)
            
            data = {
                'donate': 'on' if self.__wigle_donate else 'off'
            }

            file_form = {
                'file': (f'session_{session_id}.csv', csv)
            }

            try:
                response = requests.post(
                    url = 'https://api.wigle.net/api/v2/file/upload',
                    headers = headers,
                    data = data,
                    files = file_form,
                    timeout = 300
                )
                response.raise_for_status()
                self.__db.session_uploaded_to_wigle(session_id)
                logging.info(f'[WARDRIVER] Uploaded successfully session with id {session_id} on WiGLE')
                return True
            except Exception as e:
                logging.error(f'[WARDRIVER] Failed uploading session with id {session_id}: {e}')
                return False
        else:
            return False
    
    def on_internet_available(self, agent):
        if not self.__lock.locked() and self.ready:
            with self.__lock:
                if not self.__downloaded_assets:
                    logging.info(f'[WARDRIVER] Dowloading wardriver assets from Github')
                    self.__downloaded_assets = True
                    for asset in self.ASSETS_URL:
                        try:
                            response = requests.get(asset["url"])
                            response.raise_for_status()
                            with open(os.path.join(self.__assets_path, asset["name"]), 'wb') as f:
                                f.write(response.content)
                        except Exception as e:
                            logging.error(f'[WARDRIVER] Failed downloading {asset["name"]}: {e}')
                            self.__downloaded_assets = False

                if self.__wigle_enabled:
                    sessions_to_upload = self.__db.wigle_sessions_not_uploaded(self.__session_id)
                    if len(sessions_to_upload) > 0:
                        logging.info(f'[WARDRIVER] Uploading previous sessions on WiGLE ({len(sessions_to_upload)} sessions) - current session will not be uploaded')

                        for session_id in sessions_to_upload:
                            self.__upload_session_to_wigle(session_id)
    
    def on_webhook(self, path, request):
        if request.method == 'GET':
            if path == '/' or not path:
                return render_template_string(HTML_PAGE, plugin_version = self.__version__)
            elif path == 'current-session':
                if not self.__agent_mode or self.__agent_mode == "manual":
                    return json.dumps({
                        "id": -1,
                        "created_at": None,
                        "networks": None,
                        "last_ap_refresh": None,
                        "last_ap_reported": None,
                        'gps': self.__last_gps
                    })
                else:
                    data = self.__db.current_session_stats(self.__session_id)
                    data['last_ap_refresh'] = self.__last_ap_refresh.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if self.__last_ap_refresh else None
                    data['last_ap_reported'] = self.__last_ap_reported
                    data['gps'] = self.__last_gps
                    return json.dumps(data)
            elif path == 'general-stats':
                stats = self.__db.general_stats()
                stats['config'] = {
                    'wigle_enabled': self.__wigle_enabled,
                    'whitelist': self.__whitelist,
                    'db_path': self.__path,
                    'ui_enabled': self.__ui_enabled,
                    'wigle_api_key': self.__wigle_api_key,
                    'gps': self.__gps_config
                }
                return json.dumps(stats)
            elif "csv/" in path:
                session_id = path.split('/')[-1]
                networks = self.__db.session_networks(session_id)
                csv = self.__csv_generator.networks_to_csv(networks)
                return csv
            elif path == 'sessions':
                sessions = self.__db.sessions()
                return json.dumps(sessions)
            elif 'upload/' in path:
                session_id = path.split('/')[-1]
                result = self.__upload_session_to_wigle(session_id)
                logging.info(result)
                return '{ "status": "Success" }' if result else'{ "status": "Error! Check the logs" }'
            elif path == 'networks':
                networks = self.__db.networks()
                return json.dumps(networks)
            elif path == 'map-networks':
                networks = self.__db.map_networks()
                center = ['-', '-']
                if self.__last_gps['latitude'] != "-" and self.__last_gps['longitude'] != "-":
                    center[0] = self.__last_gps['latitude']
                    center[1] = self.__last_gps['longitude']
                elif len(networks) > 0:
                    center[0] = networks[0]['latitude']
                    center[1] = networks[0]['longitude']

                map_data = {
                    'center': center,
                    'networks': networks
                }
                return json.dumps(map_data)
            else:
                abort(404)
        abort(404)

class WardriverIcon(Widget):
    def __init__(self, path, xy, reverse, color = 0):
        super().__init__(xy, color)
        self.image = Image.open(path)
        if(reverse):
            self.image = ImageOps.invert(self.image.convert('L'))

    def draw(self, canvas, drawer):
        canvas.paste(self.image, self.xy)

HTML_PAGE = '''
{% extends "base.html" %}
{% set active_page = "plugins" %}
{% block title %}
    Wardriver
{% endblock %}

{% block meta %}
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=0" />
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/datatables/1.10.21/css/jquery.dataTables.min.css" integrity="sha512-1k7mWiTNoyx2XtmI96o+hdjP8nn0f3Z2N4oF/9ZZRgijyV4omsKOXEnqL1gKQNPy2MTSP9rIEWGcH/CInulptA==" crossorigin="anonymous" referrerpolicy="no-referrer" />
    <link
        rel="stylesheet"
        href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css"
    />
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css" integrity="sha512-DTOQO9RWCH3ppGqcWaEA1BIZOC6xxalwEsw9c2QQeAIftl+Vegovlnee1c9QX4TctnWMn13TZye+giMm8e2LwA==" crossorigin="anonymous" referrerpolicy="no-referrer" />
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
        integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
        crossorigin=""/>
    <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.4.1/dist/MarkerCluster.css" />
    <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.4.1/dist/MarkerCluster.Default.css" />

{% endblock %}

{% block styles %}
{{ super() }}
    <style>
        :root {
            --primary-color: #007bff;
            --secondary-color: #6c757d;
            --success-color: #28a745;
            --danger-color: #dc3545;
            --warning-color: #ffc107;
            --info-color: #17a2b8;
            --light-color: #f8f9fa;
            --dark-color: #343a40;
        }

        /* Light theme variables */
        [data-theme="light"] {
            --bg-color: #ffffff;
            --text-color: #333333;
            --card-bg: #ffffff;
            --border-color: #e0e0e0;
            --hover-bg: #f5f5f5;
            --alert-bg: #fff5a5;
            --alert-text: #000000;
        }

        /* Dark theme variables */
        [data-theme="dark"] {
            --bg-color: #1a1a1a;
            --text-color: #e0e0e0;
            --card-bg: #2d2d2d;
            --border-color: #404040;
            --hover-bg: #3a3a3a;
            --alert-bg: #4a4a00;
            --alert-text: #ffffff;
        }

        body {
            background-color: var(--bg-color);
            color: var(--text-color);
            transition: background-color 0.3s ease, color 0.3s ease;
        }

        .container {
            margin-top: 10px;
            margin-bottom: 30px;
            max-width: 100%;
            padding: 0 15px;
        }

        /* Mobile first responsive design */
        @media (max-width: 768px) {
            .container {
                padding: 0 10px;
            }
            
            .grid {
                grid-template-columns: 1fr !important;
                gap: 1rem;
            }
            
            #menu .grid {
                grid-template-columns: repeat(2, 1fr) !important;
            }
            
            .overflow-auto {
                font-size: 0.8rem;
            }
            
            table th, table td {
                padding: 0.5rem 0.25rem;
                white-space: nowrap;
            }
        }

        @media (max-width: 480px) {
            #menu .grid {
                grid-template-columns: 1fr !important;
            }
            
            h1 {
                font-size: 1.5rem;
            }
            
            h3 {
                font-size: 1.2rem;
            }
        }

        header i {
            font-size: 20px;
            margin-top: 10px;
            margin-right: 10px;
            color: var(--primary-color);
        }

        .center {
            text-align: center;
        }

        #menu {
            margin-top: 30px;
        }

        #menu div p {
            cursor: pointer;
            padding: 1rem;
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            transition: all 0.3s ease;
            margin: 0;
        }

        #menu div p:hover {
            background: var(--hover-bg);
            transform: translateY(-2px);
            box-shadow: 0 4px 8px rgba(0,0,0,0.1);
        }

        #menu div p.active {
            background: var(--primary-color);
            color: white;
        }

        .visible {
            display: block;
        }

        .hidden {
            display: none;
        }

        #map_networks {
            height: 60vh;
            min-height: 400px;
            width: 100%;
            border-radius: 8px;
            border: 1px solid var(--border-color);
        }

        @media (max-width: 768px) {
            #map_networks {
                height: 50vh;
                min-height: 300px;
            }
        }

        #sessions-table i, .action-icon {
            cursor: pointer;
            margin-right: 15px;
            font-size: 16px;
            padding: 5px;
            border-radius: 4px;
            transition: all 0.2s ease;
        }

        #sessions-table i:hover, .action-icon:hover {
            background: var(--hover-bg);
            transform: scale(1.1);
        }

        #manu-alert p {
            background-color: var(--alert-bg);
            padding: 15px 20px;
            text-align: center;
            margin: 20px auto;
            border-radius: 8px;
            color: var(--alert-text);
            width: fit-content;
            max-width: 90%;
            border-left: 4px solid var(--warning-color);
        }

        article {
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            transition: all 0.3s ease;
        }

        article:hover {
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
        }

        /* Theme toggle button */
        .theme-toggle {
            position: fixed;
            top: 20px;
            right: 20px;
            z-index: 1000;
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 50%;
            width: 50px;
            height: 50px;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            transition: all 0.3s ease;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }

        .theme-toggle:hover {
            transform: scale(1.1);
            box-shadow: 0 4px 12px rgba(0,0,0,0.2);
        }

        /* Dark theme specific styles */
        [data-theme="dark"] .leaflet-popup-content-wrapper {
            background: var(--card-bg);
            color: var(--text-color);
        }

        [data-theme="dark"] .leaflet-popup-tip {
            background: var(--card-bg);
        }

        [data-theme="dark"] .dataTables_wrapper {
            color: var(--text-color);
        }

        [data-theme="dark"] .dataTables_wrapper .dataTables_paginate .paginate_button {
            color: var(--text-color) !important;
            background: var(--card-bg) !important;
            border: 1px solid var(--border-color) !important;
        }

        [data-theme="dark"] .dataTables_wrapper .dataTables_paginate .paginate_button:hover {
            background: var(--hover-bg) !important;
        }

        /* Custom cluster styles for better visibility */
        .marker-cluster-small {
            background-color: rgba(181, 226, 140, 0.6);
        }
        .marker-cluster-small div {
            background-color: rgba(110, 204, 57, 0.6);
        }

        .marker-cluster-medium {
            background-color: rgba(241, 211, 87, 0.6);
        }
        .marker-cluster-medium div {
            background-color: rgba(240, 194, 12, 0.6);
        }

        .marker-cluster-large {
            background-color: rgba(253, 156, 115, 0.6);
        }
        .marker-cluster-large div {
            background-color: rgba(241, 128, 23, 0.6);
        }

        /* Loading spinner */
        .loading-spinner {
            display: inline-block;
            width: 20px;
            height: 20px;
            border: 3px solid var(--border-color);
            border-radius: 50%;
            border-top-color: var(--primary-color);
            animation: spin 1s ease-in-out infinite;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        /* Responsive tables */
        .table-responsive {
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }

        /* Status indicators */
        .status-indicator {
            display: inline-block;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            margin-right: 8px;
        }

        .status-success { background-color: var(--success-color); }
        .status-warning { background-color: var(--warning-color); }
        .status-danger { background-color: var(--danger-color); }
        .status-info { background-color: var(--info-color); }
    </style>
{% endblock %}

{% block content %}
   <div class="container" data-theme="light" id="main-container">
        <!-- Theme Toggle Button -->
        <button class="theme-toggle" id="theme-toggle" title="Toggle Dark/Light Theme">
            <i class="fas fa-moon" id="theme-icon"></i>
        </button>

        <header>
            <hgroup class="center">
                <h1>Wardriver plugin gfork</h1>
                <p>v{{ plugin_version }} by <a href="https://github.com/cyberartemio/" target="_blank">cyberartemio</a></p>
                <a href="https://discord.gg/5vrJbbW3ve" target="_blank"><i class="fa-brands fa-discord"></i></a>
                <a href="https://github.com/cyberartemio/wardriver-pwnagotchi-plugin" target="_blank"><i class="fa-brands fa-github"></i></a>
            </hgroup>
        </header>
        <main>
            <div class="grid center" id="menu">
                <div>
                    <p id="menu-current-session"><a><i class="fa-solid fa-satellite-dish"></i> Current session</a></p>
                </div>
                <div>
                    <p id="menu-stats"><a><i class="fa-solid fa-chart-line"></i> Stats</a></p>
                </div>
                <div>
                    <p id="menu-sessions"><a><i class="fa-solid fa-table"></i> Sessions</a></p>
                </div>
                <div>
                    <p id="menu-networks"><a><i class="fa-solid fa-wifi"></i> Networks</a></p>
                </div>
                <div>
                    <p id="menu-map"><a><i class="fa-solid fa-map-location-dot"></i> Map</a></p>
                </div>
            </div>
            <div id="data-container">
                <div id="current-session">
                    <h3>Current session</h3>
                    <div id="manu-alert" class="hidden">
                        <p><i class="fa-solid fa-triangle-exclamation"></i> Pwnagotchi is in MANU mode, therefore currently it's not scanning. Restart in AUTO/AI mode to start a new wardriving session</p>
                    </div>
                    <div class="grid">
                        <div>
                            <article class="center">
                                <header>Session id</header>
                                <span id="current-session-id">-</span>
                            </article>
                        </div>
                        <div>
                            <article class="center">
                                <header>Started at </header>
                                <span id="current-session-start">-</span>
                            </article>
                        </div>
                        <div>
                            <article class="center">
                                <header>Networks count</header>
                                <span id="current-session-networks">-</span>
                            </article>
                        </div>
                        <div>
                            <article class="center">
                                <header>Last APs refresh</header>
                                <span id="current-session-last-update">-</span>
                            </article>
                        </div>
                    </div>
                    <div class="grid">
                        <div>
                            <article class="center">
                                <header>Latitude</header>
                                <span id="current-session-gps-latitude">-</span>
                            </article>
                        </div>
                        <div>
                            <article class="center">
                                <header>Longitude</header>
                                <span id="current-session-gps-longitude">-</span>
                            </article>
                        </div>
                        <div>
                            <article class="center">
                                <header>Altitude</header>
                                <span id="current-session-gps-altitude">-</span>
                            </article>
                        </div>
                    </div>
                    <h4>Last APs refresh networks</h4>
                    <div class="table-responsive">
                        <table>
                            <thead>
                                <th scope="col">SSID</th>
                                <th scope="col">MAC</th>
                                <th scope="col">Channel</th>
                                <th scope="col">RSSI</th>
                                <th scope="col">Capabilities</th>
                            </thead>
                            <tbody id="current-session-table">
                                <tr><td colspan="5" class="center">No networks.</td></tr>
                            </tbody>
                        </table>
                    </div>
                    <p class="center"><i>This page will automatically refresh every 30s</i></p>
                </div>
                <div id="stats">
                    <h3>Overall</h3>
                    <div class="grid">
                        <div>
                            <article class="center">
                                <header>Networks seen</header>
                                <span id="total-networks"></span>
                            </article>
                        </div>
                        <div>
                            <article class="center">
                                <header>Sessions count</header>
                                <span id="total-sessions"></span>
                            </article>
                        </div>
                        <div>
                            <article class="center">
                                <header>Sessions uploaded</header>
                                <span id="sessions-uploaded"></span>
                            </article>
                        </div>
                    </div>
                    <div class="grid">
                        <div>
                            <h3>Your WiGLE profile</h3>
                            <article>
                                <ul>
                                    <li><b>Username</b>: <span id="wigle-username">-</span></li>
                                    <li><b>Global rank</b>: #<span id="wigle-rank">-</span></li>
                                    <li><b>Month rank</b>: #<span id="wigle-month-rank">-</span></li>
                                    <li><b>Seen WiFi</b>: <span id="wigle-seen-wifi"></span></li>
                                    <li><b>Discovered WiFi</b>: <span id="wigle-discovered-wifi">-</span></li>
                                    <li><b>WiFi this month</b>: <span id="wigle-current-month-wifi">-</span></li>
                                    <li><b>WiFi previous month</b>: <span id="wigle-previous-month-wifi">-</span></li>
                                </ul>
                                <div id="wigle-badge" class="center"></div>
                            </article>
                        </div>
                        <div>
                            <h3>Current plugin config</h3>
                            <article>
                                <ul>
                                    <li><b>WiGLE automatic upload</b>: 
                                        <span class="status-indicator" id="config-wigle-indicator"></span>
                                        <span id="config-wigle">-</span>
                                    </li>
                                    <li><b>UI enabled</b>: <span id="config-ui">-</span></li>
                                    <li><b>Database file path</b>: <span id="config-db">-</span></li>
                                    <li><b>GPS</b>:<ul id="config-gps"></ul></li>
                                    <li><b>Whitelist networks</b>:<ul id="config-whitelist"></ul></li>
                                </ul>
                            </article>
                        </div>
                    </div>
                </div>
                <div id="sessions">
                    <h3>Wardriving sessions</h3>
                    <p><b>Actions:</b><br />
                    <i class="fa-solid fa-file-csv"></i> : download session's CSV file<br />
                    <i class="fa-solid fa-cloud-arrow-up"></i> : upload session to WiGLE<br />
                    <!--<i class="fa-solid fa-trash"></i> : delete the session (<b>not the networks</b>)-->
                    </p>
                    <div class="table-responsive">
                        <table>
                            <thead>
                                <th scope="col">ID</th>
                                <th scope="col">Date</th>
                                <th scope="col">Networks</th>
                                <th scope="col">Uploaded</th>
                                <th scope="col">Actions</th>
                            </thead>
                            <tbody id="sessions-table">
    
                            </tbody>
                        </table>
                    </div>
                </div>
                <div id="networks">
                    <h3>Networks</h3>
                    <div class="table-responsive">
                        <table id="networks-table-container">
                            <thead>
                                <th scope="col">ID</th>
                                <th scope="col">MAC</th>
                                <th scope="col">SSID</th>
                                <th scope="col">First seen</th>
                                <th scope="col">First session ID</th>
                                <th scope="col">Last seen</th>
                                <th scope="col">Last session ID</th>
                                <th scope="col"># sessions</th>
                            </thead>
                            <tbody id="networks-table">
    
                            </tbody>
                        </table>
                    </div>
                </div>
                <div id="map">
                    <h3>Networks map</h3>
                    <p class="center"><i><i class="fa-solid fa-lightbulb"></i> Tip: click on a cluster or marker to see the networks discovered there</i></p>
                    <div id="map-loading" class="center" style="padding: 20px;">
                        <div class="loading-spinner"></div>
                        <p>Loading map data...</p>
                    </div>
                    <div id="map_networks"></div>
                </div>
            </div>
        </main>
        <footer>

        </footer>
    </div>
{% endblock %}
{% block script %}
    <script src="https://cdnjs.cloudflare.com/ajax/libs/jquery/3.7.1/jquery.min.js" integrity="sha512-v2CJ7UaYy4JwqLDIrZUI/4hqeoQieOmAZNXBeQyjo21dadnwR+8ZaIJVT8EE2iyI61OV8e6M8PP2/4hpQINQ/g==" crossorigin="anonymous" referrerpolicy="no-referrer"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/datatables/1.10.21/js/jquery.dataTables.min.js" integrity="sha512-BkpSL20WETFylMrcirBahHfSnY++H2O1W+UnEEO4yNIl+jI2+zowyoGJpbtk6bx97fBXf++WJHSSK2MV4ghPcg==" crossorigin="anonymous" referrerpolicy="no-referrer"></script>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
        crossorigin=""></script>
    <script src="https://unpkg.com/leaflet.markercluster@1.4.1/dist/leaflet.markercluster-src.js"></script>
    <script>
    (function() {
        container = document.getElementById("data-container")
        setupMenuClickListeners()
        setupThemeToggle()
        showCurrentSession()
        var map
        var markersLayer

        // Theme management
        function setupThemeToggle() {
            const themeToggle = document.getElementById('theme-toggle')
            const themeIcon = document.getElementById('theme-icon')
            const mainContainer = document.getElementById('main-container')
            
            // Load saved theme or default to light
            const savedTheme = localStorage.getItem('wardriver-theme') || 'light'
            setTheme(savedTheme)
            
            themeToggle.addEventListener('click', function() {
                const currentTheme = mainContainer.getAttribute('data-theme')
                const newTheme = currentTheme === 'light' ? 'dark' : 'light'
                setTheme(newTheme)
                localStorage.setItem('wardriver-theme', newTheme)
            })
            
            function setTheme(theme) {
                mainContainer.setAttribute('data-theme', theme)
                themeIcon.className = theme === 'light' ? 'fas fa-moon' : 'fas fa-sun'
                
                // Update map tiles if map exists
                if (map && map.hasLayer) {
                    updateMapTheme(theme)
                }
            }
        }

        function updateMapTheme(theme) {
            if (!map) return
            
            // Remove existing tile layer
            map.eachLayer(function(layer) {
                if (layer instanceof L.TileLayer) {
                    map.removeLayer(layer)
                }
            })
            
            // Add appropriate tile layer based on theme
            const tileUrl = theme === 'dark' 
                ? 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png'
                : 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
            
            const attribution = theme === 'dark'
                ? '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
                : '&copy; <a href="http://www.openstreetmap.org/copyright">OpenStreetMap</a>'
            
            L.tileLayer(tileUrl, {
                maxZoom: 19,
                attribution: attribution
            }).addTo(map)
        }

        function downloadCSV(session_id) {
            request("GET", "/plugins/wardriver/csv/" + session_id, function(text) {
                const blob = new Blob([text], { type: 'text/csv' })
                const url = window.URL.createObjectURL(blob) 
                const a = document.createElement('a')
                a.setAttribute('href', url) 
                a.setAttribute('download', `wardriver-session-${session_id}.csv`)
                a.click()
                window.URL.revokeObjectURL(url)
            })
        }

        function uploadSessionsToWigle(session_id) {
            const button = event.target
            const originalHtml = button.innerHTML
            button.innerHTML = '<i class="loading-spinner"></i>'
            button.disabled = true
            
            request('GET', '/plugins/wardriver/upload/' + session_id, function(message) {
                button.innerHTML = originalHtml
                button.disabled = false
                showSessions()
                alert(message.status)
            })
        }

        function getCurrentSessionStats() {
            request('GET', "/plugins/wardriver/current-session", function(data) {
                if(data.id == -1) {
                    document.getElementById("manu-alert").className = 'visible'
                    document.getElementById("current-session-id").innerHTML = '-'
                    document.getElementById("current-session-networks").innerHTML = '-'
                    document.getElementById("current-session-last-update").innerHTML = '-'
                    document.getElementById("current-session-start").innerHTML = '-'
                    return
                }

                document.getElementById("current-session-gps-latitude").innerHTML = data.gps.latitude || '-'
                document.getElementById("current-session-gps-longitude").innerHTML = data.gps.longitude || '-'
                document.getElementById("current-session-gps-altitude").innerHTML = data.gps.altitude || '-'

                document.getElementById("manu-alert").className = 'hidden'
                document.getElementById("current-session-id").innerHTML = data.id
                document.getElementById("current-session-networks").innerHTML = data.networks
                document.getElementById("current-session-last-update").innerHTML = data.last_ap_refresh ? "<time class='timeago' datetime='" + parseUTCDate(data.last_ap_refresh).toISOString() + "'>-</time>" : "-"
                var sessionStartDate = parseUTCDate(data.created_at)
                document.getElementById("current-session-start").innerHTML = ("0" + sessionStartDate.getHours()).slice(-2) + ":" + ("0" + sessionStartDate.getMinutes()).slice(-2)
                var apTable = document.getElementById("current-session-table")
                apTable.innerHTML = ""
                if(data.last_ap_reported.length == 0) {
                    var tableRow = document.createElement('tr')
                    tableRow.innerHTML = "<td colspan='5' class='center'>No networks.</td>"
                    apTable.appendChild(tableRow)
                }
                else {
                    for(var network of data.last_ap_reported) {
                        var tableRow = document.createElement('tr')
                        var ssidCol = document.createElement('td')
                        var macCol = document.createElement('td')
                        var channelCol = document.createElement('td')
                        var rssiCol = document.createElement('td')
                        var capabilitiesCol = document.createElement('td')
                        
                        ssidCol.innerText = network.ssid || 'Hidden'
                        macCol.innerText = network.mac
                        channelCol.innerText = network.channel
                        rssiCol.innerText = network.rssi + ' dBm'
                        capabilitiesCol.innerText = network.capabilities
                        
                        // Add RSSI color coding
                        const rssiValue = parseInt(network.rssi)
                        if (rssiValue > -50) rssiCol.style.color = 'var(--success-color)'
                        else if (rssiValue > -70) rssiCol.style.color = 'var(--warning-color)'
                        else rssiCol.style.color = 'var(--danger-color)'
                        
                        tableRow.appendChild(ssidCol)
                        tableRow.appendChild(macCol)
                        tableRow.appendChild(channelCol)
                        tableRow.appendChild(rssiCol)
                        tableRow.appendChild(capabilitiesCol)
                        apTable.appendChild(tableRow)
                    }
                }
                if (typeof jQuery !== 'undefined' && jQuery("time.timeago").length) {
                    jQuery("time.timeago").timeago();
                }
            })
        }

        setInterval(getCurrentSessionStats, 30 * 1000) // refresh current session data every 30s
        
        // Make HTTP request to pwnagotchi "server"
        function request(method, url, callback, errorCallback) {
            var xobj = new XMLHttpRequest();
            xobj.overrideMimeType("application/json")
            xobj.open(method, url, true);
            xobj.onreadystatechange = function () {
                if (xobj.readyState == 4) {
                    if (xobj.status == "200") {
                        var response = xobj.responseText
                        try {
                            response = JSON.parse(xobj.responseText)
                        }
                        catch(error) {
                            // Response is not JSON, keep as text
                        }
                        callback(response)
                    } else if (errorCallback) {
                        errorCallback(xobj.status, xobj.statusText)
                    }
                }
            }
            xobj.send(null);
        }

        function loadWigleStats(api_key, callback) {
            var xobj = new XMLHttpRequest();
            xobj.overrideMimeType("application/json")
            xobj.open("GET", "https://api.wigle.net/api/v2/stats/user", true);
            xobj.setRequestHeader("Authorization", "Basic " + api_key)
            xobj.onreadystatechange = function () {
                if (xobj.readyState == 4 && xobj.status == "200") {
                    callback(JSON.parse(xobj.responseText))
                }
            }
            xobj.send(null);
        }

        function parseUTCDate(date) {
            var utcDateStr = date.replace(" ", "T")
            utcDateStr += ".000Z"
            return new Date(utcDateStr)
        }

        function updateContainerView(showing) {
            var views = [
                "current-session",
                "stats",
                "sessions",
                "networks",
                "map"
            ]

            // Remove active class from all menu items
            document.querySelectorAll('#menu p').forEach(function(item) {
                item.classList.remove('active')
            })

            // Add active class to current menu item
            document.getElementById('menu-' + showing).classList.add('active')

            // Destroy DataTable if it exists
            if ($.fn.DataTable && $.fn.DataTable.isDataTable('#networks-table-container')) {
                $("#networks-table-container").DataTable().destroy();
            }

            for(var view of views)
                document.getElementById(view).className = view == showing ? "visible" : "hidden"
        }

        function showCurrentSession() {
            updateContainerView("current-session")
            getCurrentSessionStats()
        }

        function showStats() {
            updateContainerView("stats")
            request('GET', "/plugins/wardriver/general-stats", function(data) {
                document.getElementById("total-networks").innerText = data.total_networks
                document.getElementById("total-sessions").innerText = data.total_sessions
                document.getElementById("sessions-uploaded").innerText = data.sessions_uploaded
                
                // Update config with status indicators
                const wigleEnabled = data.config.wigle_enabled
                document.getElementById("config-wigle").innerText = wigleEnabled ? "enabled" : "disabled"
                document.getElementById("config-wigle-indicator").className = `status-indicator ${wigleEnabled ? 'status-success' : 'status-danger'}`
                
                document.getElementById("config-ui").innerText = data.config.ui_enabled
                document.getElementById("config-db").innerText = data.config.db_path
                
                document.getElementById("config-whitelist").innerHTML = ""
                if(data.config.whitelist.length == 0)
                    document.getElementById("config-whitelist").innerHTML = "none"
                else
                    for(var network of data.config.whitelist) {
                        var item = document.createElement("li")
                        item.innerHTML = "<code>" + network + "</code>"
                        document.getElementById("config-whitelist").appendChild(item)
                    }

                document.getElementById("config-gps").innerHTML = ""
                var gps_method = document.createElement("li")
                gps_method.innerHTML = "Method: <code>" + data.config.gps.method + "</code>"
                document.getElementById("config-gps").appendChild(gps_method)
                if(data.config.gps.method != "bettercap") {
                    var host = document.createElement("li")
                    host.innerHTML = "Host: <code>" + data.config.gps.host + "</code>"
                    document.getElementById("config-gps").appendChild(host)
                    var port = document.createElement("li")
                    port.innerHTML = "Port: <code>" + data.config.gps.port + "</code>"
                    document.getElementById("config-gps").appendChild(port)
                }
                
                if(data.config.wigle_api_key) {
                    loadWigleStats(data.config.wigle_api_key, function(stats) {
                        document.getElementById("wigle-username").innerText = stats.user
                        document.getElementById("wigle-rank").innerText = stats.rank
                        document.getElementById("wigle-month-rank").innerText = stats.monthRank
                        document.getElementById("wigle-seen-wifi").innerText = stats.statistics.discoveredWiFi
                        document.getElementById("wigle-discovered-wifi").innerText = stats.statistics.discoveredWiFiGPS
                        document.getElementById("wigle-current-month-wifi").innerText = stats.statistics.eventMonthCount
                        document.getElementById("wigle-previous-month-wifi").innerText = stats.statistics.eventPrevMonthCount
                        document.getElementById("wigle-badge").innerHTML = "<img src='https://wigle.net" + stats.imageBadgeUrl +"' alt='wigle-profile-badge' style='max-width: 100%; height: auto;' />"
                    })
                }
            })
        }

        function showSessions() {
            updateContainerView("sessions")
            request('GET', "/plugins/wardriver/sessions", function(data) {
                var sessionsTable = document.getElementById("sessions-table")
                sessionsTable.innerHTML = ""
                for(var session of data) {
                    var tableRow = document.createElement("tr")
                    var idCol = document.createElement("td")
                    var createdCol = document.createElement("td")
                    var networksCol = document.createElement("td")
                    var wigleCol = document.createElement("td")
                    var actionsCol = document.createElement("td")

                    idCol.innerHTML = session.id
                    createdCol.innerHTML = new Date(session.created_at).toLocaleDateString() + ' ' + new Date(session.created_at).toLocaleTimeString()
                    networksCol.innerHTML = session.networks
                    wigleCol.innerHTML = "<span class='status-indicator " + (session.wigle_uploaded ? "status-success" : "status-warning") + "'></span><i class='fa-regular " + (session.wigle_uploaded ? "fa-square-check" : "fa-square") + "'></i>"
                    
                    csvIcon = document.createElement('i')
                    csvIcon.className = 'fa-solid fa-file-csv action-icon'
                    csvIcon.title = 'Download CSV'
                    csvIcon.addEventListener("click", function(session_id) { return function() { downloadCSV(session_id)} } (session.id))
                    
                    actionsCol.appendChild(csvIcon)
                    
                    if(!session.wigle_uploaded) {
                        wigleIcon = document.createElement('i')
                        wigleIcon.className = 'fa-solid fa-cloud-arrow-up action-icon'
                        wigleIcon.title = 'Upload to WiGLE'
                        wigleIcon.addEventListener("click", function(session_id) { return function() { uploadSessionsToWigle(session_id)} } (session.id))
                        actionsCol.appendChild(wigleIcon)
                    }
                    
                    tableRow.appendChild(idCol)
                    tableRow.appendChild(createdCol)
                    tableRow.appendChild(networksCol)
                    tableRow.appendChild(wigleCol)
                    tableRow.appendChild(actionsCol)
                    sessionsTable.appendChild(tableRow)
                }
            })
        }

        function showNetworks() {
            updateContainerView("networks")
            request('GET', "/plugins/wardriver/networks", function(data) {
                $('#networks-table-container').DataTable({
                    data: data,
                    searching: true,
                    lengthChange: true,
                    pageLength: 25,
                    responsive: true,
                    columns: [
                        { data: "id", width: "5%" },
                        { data: "mac", width: "15%" },
                        { data: "ssid", width: "20%", render: function(data) {
                            return data || '<em>Hidden</em>'
                        }},
                        { data: "first_seen", width: "15%", render: function(data) {
                            return new Date(data).toLocaleDateString()
                        }},
                        { data: "first_session", width: "10%" },
                        { data: "last_seen", width: "15%", render: function(data) {
                            return new Date(data).toLocaleDateString()
                        }},
                        { data: "last_session", width: "10%" },
                        { data: "sessions_count", width: "10%" }
                    ]
                })
            })
        }

        function showMap() {
            updateContainerView("map")
            document.getElementById("map-loading").style.display = 'block'
            document.getElementById("map_networks").style.display = 'none'
            
            request('GET', '/plugins/wardriver/map-networks', function(response) {
                var networks = response.networks
                var center = response.center
                
                document.getElementById("map-loading").style.display = 'none'
                document.getElementById("map_networks").style.display = 'block'
                
                if(center[0] == "-" || center[1] == "-") {
                    if(navigator.geolocation) {
                        navigator.geolocation.getCurrentPosition(function(position) {
                            center[0] = position.coords.latitude
                            center[1] = position.coords.longitude
                            renderMap(networks, center)
                        }, function() {
                            center[0] = 51.505
                            center[1] = -0.09
                            renderMap(networks, center)
                        })
                    }
                    else {
                        center[0] = 51.505
                        center[1] = -0.09
                        renderMap(networks, center)
                    }
                }
                else {
                    renderMap(networks, center)
                }
            }, function(status, statusText) {
                document.getElementById("map-loading").innerHTML = '<p style="color: var(--danger-color);">Error loading map data: ' + statusText + '</p>'
            })
        }

        function renderMap(networks, center) {
            // Remove existing map if it exists
            if(map) {
                map.remove()
            }
            
            // Initialize map
            map = L.map("map_networks", { 
                center: center, 
                zoom: 13, 
                zoomControl: true,
                preferCanvas: true
            })
            
            // Get current theme and add appropriate tile layer
            const currentTheme = document.getElementById('main-container').getAttribute('data-theme')
            updateMapTheme(currentTheme)
            
            // Create marker cluster group with custom options
            markersLayer = L.markerClusterGroup({
                chunkedLoading: true,
                chunkProgress: function(processed, total, elapsed) {
                    // Optional: show progress
                },
                maxClusterRadius: 50,
                spiderfyOnMaxZoom: true,
                showCoverageOnHover: false,
                zoomToBoundsOnClick: true
            })

            // Custom icon for network markers
            var networkIcon = L.divIcon({
                className: 'network-marker',
                html: '<i class="fas fa-wifi" style="color: #007bff; font-size: 16px;"></i>',
                iconSize: [20, 20],
                iconAnchor: [10, 10],
                popupAnchor: [0, -10]
            })

            // Group networks by location to avoid overlapping markers
            var networksGrouped = networks.reduce(function (grouped, network) {
                // Round coordinates to avoid micro-differences
                var lat = parseFloat(network.latitude).toFixed(6)
                var lng = parseFloat(network.longitude).toFixed(6)
                var key = lat + "," + lng
                grouped[key] = grouped[key] || []
                grouped[key].push(network)
                return grouped
            }, Object.create(null))
            
            var markers = []
            var bounds = L.latLngBounds()
            
            Object.keys(networksGrouped).forEach(key => {
                var networksAtLocation = networksGrouped[key]
                var coordinates = key.split(",")
                var lat = parseFloat(coordinates[0])
                var lng = parseFloat(coordinates[1])
                
                if (isNaN(lat) || isNaN(lng)) return
                
                bounds.extend([lat, lng])
                
                // Create popup content
                var popupContent = "<div style='max-height: 200px; overflow-y: auto;'>"
                popupContent += "<h4>Networks at this location (" + networksAtLocation.length + ")</h4>"
                
                var displayCount = Math.min(networksAtLocation.length, 10)
                for (var i = 0; i < displayCount; i++) {
                    var network = networksAtLocation[i]
                    var ssid = network.ssid || '<em>Hidden Network</em>'
                    var security = network.capabilities ? 
                        (network.capabilities.includes('WPA') ? '<span style="color: orange;">🔒 WPA</span>' : 
                         network.capabilities.includes('WEP') ? '<span style="color: red;">🔒 WEP</span>' : 
                         '<span style="color: green;">🔓 Open</span>') : 
                        '<span style="color: gray;">Unknown</span>'
                    
                    popupContent += "<div style='border-bottom: 1px solid #eee; padding: 5px 0;'>"
                    popupContent += "<strong>" + ssid + "</strong><br/>"
                    popupContent += "<small>MAC: " + network.mac + "</small><br/>"
                    popupContent += "<small>Security: " + security + "</small>"
                    if (network.channel) {
                        popupContent += "<small> | Channel: " + network.channel + "</small>"
                    }
                    popupContent += "</div>"
                }
                
                if (networksAtLocation.length > displayCount) {
                    popupContent += "<p><em>+" + (networksAtLocation.length - displayCount) + " more networks...</em></p>"
                }
                popupContent += "</div>"
                
                // Create marker
                var marker = L.marker([lat, lng], {icon: networkIcon})
                    .bindPopup(popupContent, {
                        maxWidth: 300,
                        className: 'network-popup'
                    })
                
                markers.push(marker)
            })

            // Add markers to cluster group
            markersLayer.addLayers(markers)
            map.addLayer(markersLayer)
            
            // Fit map to bounds if we have markers
            if (markers.length > 0) {
                map.fitBounds(bounds, {padding: [10, 10]})
            }
            
            // Add scale and attribution controls
            L.control.scale().addTo(map)
            
            // Add custom control for network count
            var networkCountControl = L.control({position: 'topright'})
            networkCountControl.onAdd = function(map) {
                var div = L.DomUtil.create('div', 'network-count-control')
                div.style.background = 'var(--card-bg)'
                div.style.padding = '5px 10px'
                div.style.borderRadius = '4px'
                div.style.border = '1px solid var(--border-color)'
                div.innerHTML = '<strong>' + networks.length + '</strong> networks'
                return div
            }
            networkCountControl.addTo(map)
        }

        function setupMenuClickListeners() {
            document.getElementById("menu-current-session").addEventListener("click", showCurrentSession)
            document.getElementById("menu-stats").addEventListener("click", showStats)
            document.getElementById("menu-sessions").addEventListener("click", showSessions)
            document.getElementById("menu-networks").addEventListener("click", showNetworks)
            document.getElementById("menu-map").addEventListener("click", showMap)
        }

        // Initialize with current session view
        updateContainerView("current-session")
    })()
    </script>
{% endblock %}
'''