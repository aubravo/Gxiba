import datetime
import enum
import json
import logging
import os
import re

from pathlib import Path
from typing import Optional, List, ClassVar

import dask
import fsspec
import jinja2
import numpy as np
import rasterio
from PIL import Image
from google.api_core import retry
from google.cloud import bigquery
from google.oauth2 import service_account
from pydantic import Field, AnyUrl
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, func, String, Enum, Integer, Column, Float, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import scoped_session, sessionmaker, DeclarativeBase, MappedAsDataclass, Mapped, mapped_column, relationship, Session
from satpy import Scene

from exceptions import ConfigFileNotFound

logger = logging.getLogger(__name__)
sql_environment = jinja2.Environment(loader=jinja2.FileSystemLoader('queries/'))


class Base(DeclarativeBase, MappedAsDataclass):
    pass


class RadiometricMeasure(enum.Enum):
    RADIANCE = "RADIANCE"
    REFLECTANCE = "REFLECTANCE"
    DN = "DN"


class AtmosphericReferenceLevel(enum.Enum):
    TOA = "TOA"
    BOA = "BOA"


class FileProcessingStatus(enum.Enum):
    FOUND = "FOUND"
    STORING_BAND = "STORING_BAND"
    STORED_BAND = "STORED_BAND"
    BAND_STORAGE_FAILED = "BAND_STORAGE_FAILED"


class OutputStorageType(enum.Enum):
    GCS = "GCS"
    LOCAL = "LOCAL"
    VOLATILE = "VOLATILE"
    # ToDo: Implement further output storage types


class CaptureData(Base):
    __tablename__ = "capture_data"
    
    main_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    secondary_id: Mapped[str] = mapped_column(String(30))
    mission_id: Mapped[str] = mapped_column(String(30))
    sensing_time: Mapped[DateTime] = mapped_column(DateTime)
    north_lat: Mapped[float]
    south_lat: Mapped[float]
    west_lon: Mapped[float]
    east_lon: Mapped[float]
    base_url: Mapped[str] = mapped_column(String(120))
    cloud_cover: Mapped[Optional[float]] = None
    radiometric_measure: Mapped[Optional[str]]  = mapped_column(Enum(RadiometricMeasure), default=None)
    athmospheric_reference_level: Mapped[Optional[str]] = mapped_column(Enum(AtmosphericReferenceLevel), default=None)
    mgrs_tile: Mapped[Optional[str]] = mapped_column(String(30), default=None)
    wrs_path: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    wrs_row: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    data_type: Mapped[Optional[str]] = mapped_column(String(30), default=None)
    

    # Relationships
    source_files: Mapped[Optional[List["SourceFile"]]] = relationship("SourceFile", default_factory=list)
    
    # Templated columns
    added_timestamp: Mapped[datetime.datetime] = mapped_column(default_factory=datetime.datetime.now)
    last_updated_timestamp: Mapped[datetime.datetime] = mapped_column(default_factory=datetime.datetime.now, onupdate=datetime.datetime.now)
    schema = "earthgazer"
    
    @classmethod
    def get_by_main_id(cls, session: Session, main_id: str) -> "CaptureData":
        return session.query(cls).filter(cls.main_id == main_id).first()

    def add_source_file(self, src_file: "SourceFile") -> None:
        self.source_files.append(src_file)


class SourceFile(Base):
    __tablename__ = "source_file"
    
    main_id: Mapped[str] = mapped_column(ForeignKey("capture_data.main_id"), primary_key=True)
    file_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    format: Mapped[str] = mapped_column(String(30))
    source_image_url: Mapped[str] = mapped_column(String(250))

    # Templated columns
    added_timestamp: Mapped[datetime.datetime] = mapped_column(default_factory=datetime.datetime.now)
    last_updated_timestamp: Mapped[datetime.datetime] = mapped_column(default_factory=datetime.datetime.now, onupdate=datetime.datetime.now)
    schema = "earthgazer"


class Location(Base):
    __tablename__ = "location"
    
    location_name: Mapped[str] = mapped_column(String(50))
    location_description: Mapped[str] = mapped_column(String(500))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    location_id: int = Column(Integer, primary_key=True, autoincrement=True)
    monitoring_period_start: Mapped[Optional[datetime.date]] = mapped_column(default=datetime.datetime.fromisoformat('1950-01-01'))
    monitoring_period_end: Mapped[Optional[datetime.date]] = mapped_column(default=datetime.date.fromisoformat('2050-12-12'))

    # Templated columns
    added_timestamp: Mapped[datetime.datetime] = mapped_column(default_factory=datetime.datetime.now)
    last_updated_timestamp: Mapped[datetime.datetime] = mapped_column(default_factory=datetime.datetime.now, onupdate=datetime.datetime.now)
    schema = "earthgazer"


class EGConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='eg_')

    # Local environment
    local_path: str = f'{os.path.expanduser("~")}/.eg'.replace('\\', '/')
    
    # Pipeline steps
    update_data: bool = False
    download_band_images: bool = False
    composite_generation: bool = False
    normalize_dataset: bool = False
    track_images: bool = False

    # Data
    force_download: bool = False
    storage_type: str = Field(default='local')
    local_storage_base_path: str = f'{local_path}/storage'
    cloud_backup_bucket: str = 'gxiba-storage'
    remote_storage_base_path: str = f'{cloud_backup_bucket}/eg/storage'
    google_credentials_file_path: str = f'{local_path}/credentials.json'
    google_storage_downloader_retry_inital: int = 1
    google_storage_downloader_retry_maximum: int = 120
    google_storage_downloader_retry_multiplier: int = 2
    google_storage_downloader_retry_timeout: int = 1200

    # Database
    database_url: AnyUrl = Field(default=f'sqlite:///{local_path}/eg.db', exclude=True)
    database_type: ClassVar[str] = str(database_url).split(':')[0]
    database_engine_echo: bool = False
    
    # Platforms
    monitored_platforms: list = ['LANDSAT_8', 'SENTINEL_2']
    

class BandProcessor:
    @staticmethod
    def open_band(band_path):
        with rasterio.open(band_path) as src:
            return src.read(1)

    @staticmethod
    def save_image(image, output_path):
        image = np.interp(image, (image.min(), image.max()), (0, 255)).astype(np.uint8)
        im = Image.fromarray(image)
        im.save(output_path, 'JPEG', quality=100)


class EGProcessor:
    def __init__(self):
        welcome_message = ''
        with open('static/ascii_logo.txt', 'r', encoding='utf-8') as f:
            self.logo = f.read()
            welcome_message += self.logo
            welcome_message += '\n'
        with open('static/short_licence_disclaimer.txt', 'r', encoding='utf-8') as f:
            self.short_licence = f.read()
            welcome_message += self.short_licence
        logger.info(welcome_message)

        logger.debug('Loading configuration...')
        self.env = EGConfig()
        dump = '\n'
        for key, value in self.env.model_dump().items():
            dump += f'   {key + ":": <45} {value}\n'
        logger.debug(dump)

        Path(self.env.local_path).mkdir(parents=True, exist_ok=True)
        Path(self.env.local_storage_base_path).mkdir(parents=True, exist_ok=True)

        def load_config(config_file: str) -> dict:
            config_path = os.path.join('config', config_file + '.json')
            try:
                with open(config_path, 'r') as f:
                    return json.load(f)
            except FileNotFoundError:
                logger.error(f'{config_file} file not found')
                raise ConfigFileNotFound(f'{config_file} file not found')

        self.bigquery_platforms_config = load_config('bigquery_platforms')
        self.bands_definition = load_config('bands')
        self.composites_definition = load_config('composites')

        with open(self.env.google_credentials_file_path, 'r') as f:
            service_account_credentials = service_account.Credentials.from_service_account_info(
                json.load(f),
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )

        self.bigquery_client = bigquery.Client(
            credentials=service_account_credentials,
            project=service_account_credentials.project_id
        )
        

        # TODO
        # Add logic to handle multiple types of filesystems
        self.gcs_storage_options = {"token": self.env.google_credentials_file_path}
        self.gcs_storage_client = fsspec.filesystem('gcs', **self.gcs_storage_options)

        self.retry_methodology = retry.Retry(
            initial=self.env.google_storage_downloader_retry_inital,
            maximum=self.env.google_storage_downloader_retry_maximum,
            multiplier=self.env.google_storage_downloader_retry_multiplier,
            timeout=self.env.google_storage_downloader_retry_timeout
        )

        engine = create_engine(str(self.env.database_url), echo=self.env.database_engine_echo)
        Base.metadata.create_all(engine)
        self.session_factory = sessionmaker(bind=engine)

    def add_location(self, **kwargs):
        session = scoped_session(self.session_factory)
        try:
            location_data = kwargs
            for key in location_data:
                if 'monitoring_period' in key:
                    location_data[key] = datetime.date.fromisoformat(location_data[key])
            location_data['location_id'] = session.query(func.coalesce(func.max(Location.location_id),0)).scalar() + 1
            location = Location(**location_data)
            session.add(location)
            session.commit()
        finally:
            session.close()
        
    def update_bigquery_data(self):
        session = scoped_session(self.session_factory)
        logger.info('Beginning data update process')
        try:
            for platform_config in self.bigquery_platforms_config:
                logger.info(f'Updating {platform_config.capitalize()} platform data')
                for location in session.query(Location).where(Location.active == True):
                    logger.info(f'Updating {location.location_name.capitalize()} location data')
                    platform_query_params = self.bigquery_platforms_config[platform_config]
                    platform_query_params.update({'lat': location.latitude,
                                                  'lon': location.longitude,
                                                  'start_date': location.monitoring_period_start,
                                                  'end_date': location.monitoring_period_end })
                    platform_query = sql_environment.get_template('bigquery_get_locations.sql').render(**platform_query_params)
                    logger.info(platform_query)
                    for result in self.bigquery_client.query(platform_query):
                        if session.query(CaptureData).where(CaptureData.main_id == result["main_id"]).count() > 0:
                            logger.debug(f'{result["main_id"]} already in database')
                            continue
                        logger.debug(f'Adding {result["main_id"]} to database')
                        session.add(CaptureData(**result))
                        session.commit()
        finally:
            session.close()

    def get_source_file_data(self):
        
        gcs_url_parser = re.compile(r"gs://(?P<bucket_name>.*?)/(?P<blobs_path_name>.*)")
        blob_finder = re.compile(r"^.*?(?:tiles.*?IMG_DATA.*?|/LC0[0-9]_.*?)_(?P<file_id>B[0-9A]{1,2}|MTL)\.(?P<format>TIF|jp2|txt)$")
        
        def mission_parse(captured_data_element: CaptureData) -> str:
                    if 'SENTINEL-2' in captured_data_element.mission_id:
                        return 'SENTINEL_2'
                    elif 'LANDSAT_8' in captured_data_element.mission_id:
                        return 'LANDSAT_8'
                    else:
                        logger.warning(f'{captured_data_element.mission_id} for {captured_data_element.main_id} is currently not supported')
                        return None

        logger.info('Beginning band images download process')
        session = scoped_session(self.session_factory)
        try:
            for captured_data_element in session.query(CaptureData).all():
                if (mission:= mission_parse(captured_data_element)) is None:
                    continue
                if len(captured_data_element.source_files) < len(self.bands_definition[mission]):
                    logger.debug(f'{captured_data_element.main_id} from platform {captured_data_element.mission_id} has missing bands for download')
                    parsed_url = gcs_url_parser.search(captured_data_element.base_url).groupdict()
                    logger.info({captured_data_element.base_url})
                    for blob in self.gcs_storage_client.find(f'{captured_data_element.base_url}/'):
                        if test := blob_finder.search(blob):
                            logger.debug(f'Found file with matching name requirements: {blob}')
                            file_id, format = test.groupdict()['file_id'], test.groupdict()['format']
                            logger.debug(f'Found {file_id} {format} for {captured_data_element.main_id}')
                            if session.query(SourceFile).where(SourceFile.main_id == captured_data_element.main_id).where(SourceFile.file_id == file_id).count() > 0:
                                logger.warning(f'{file_id} {format} for {captured_data_element.main_id} already in database')
                                continue
                            source_file = SourceFile(main_id=captured_data_element.main_id,
                                                     file_id= file_id,
                                                     format=format,
                                                     source_image_url=f'gs://{blob}')
                            captured_data_element.add_source_file(source_file)
                            session.commit()
                else:
                    logger.debug(f'Bands for {captured_data_element.main_id} already downloaded')
                    continue
        finally:
            session.close()

    
    def backup_images(self):
        present_files =self.gcs_storage_client.find(f'gs://{self.env.remote_storage_base_path}/')
        session = scoped_session(self.session_factory)
        try:
            for source_file in session.query(SourceFile):
                sourcefile_name = source_file.source_image_url.split('/')[-1]
                if f'{self.env.remote_storage_base_path}/{source_file.main_id}/{sourcefile_name}' not in present_files:
                    save_path = f'gs://{self.env.remote_storage_base_path}/{source_file.main_id}/{sourcefile_name}'
                    self.gcs_storage_client.copy(source_file.source_image_url, save_path)
                else:
                    logger.debug(f'{source_file.main_id} already backed up.')


        finally:
            session.close()
        
    


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG, format='[%(levelname)s] - %(asctime)s - %(message)s')
    eg = EGProcessor()
    if False:
        import os
        with open(os.path.curdir + '/test/popocatepetl.json', 'r') as f:
            test = json.load(f)
        eg.add_location(**test)
        eg.update_bigquery_data()
        eg.get_source_file_data()
    eg.backup_images()