from __future__ import annotations
from datetime import timedelta
import logging

REGION_EUROPE = "Europe"

REGION_NORAM = "North America"

REGION_APAC = "Asia-Pacific"

REGION_CHINA = "China"

CONF_EXCLUDED_CARS = "excluded_cars"

CONF_PIN = "pin"

CONF_DEBUG_FILE_SAVE = "save_files"

CONF_FT_DISABLE_CAPABILITY_CHECK = "cap_check_disabled"

DOMAIN = "mbapi2020"

DEFAULT_CACHE_PATH = "custom_components/mbapi2020/messages"

DEFAULT_DOWNLOAD_PATH = "custom_components/mbapi2020/resources"

DEFAULT_LOCALE = "en-GB"

DEFAULT_COUNTRY_CODE = "EN"

JSON_EXPORT_IGNORED_KEYS = (
    "pin",
    "access_token",
    "refresh_token",
    "username",
    "unique_id",
    "nonce",
    "_update_listeners",
    "finorvin",
    "licenseplate",
    "fin",
    "licensePlate",
    "vin",
    "dealers",
    "positionLong",
    "positionHeading",
    "id_token",
    "password",
    "title",
    "device_guid",
    "deviceGuid",
)

RIS_APPLICATION_VERSION_NA = "3.71.0"

RIS_APPLICATION_VERSION_CN = "1.67.0"

RIS_APPLICATION_VERSION_PA = "1.71.0"

RIS_APPLICATION_VERSION = "1.73.0"

RIS_SDK_VERSION = "4.20.1"

RIS_SDK_VERSION_CN = "2.132.2"

RIS_OS_VERSION = "26.3"

RIS_OS_NAME = "ios"

USE_PROXY = False

VERIFY_SSL = True

SYSTEM_PROXY: str | None = None if not USE_PROXY else "http://192.168.178.68:9090"

LOGIN_APP_ID_EU = "62778dc4-1de3-44f4-af95-115f06a3a008"

LOGIN_APP_ID_CN = "3f36efb1-f84b-4402-b5a2-68a118fec33e"

LOGIN_BASE_URI = "https://id.mercedes-benz.com"

LOGIN_BASE_URI_CN = "https://ciam-1.mercedes-benz.com.cn"

PSAG_BASE_URI = "https://psag.query.api.dvb.corpinter.net"

PSAG_BASE_URI_CN = "https://psag.query.api.dvb.corpinter.net.cn"

RCP_BASE_URI = "https://rcp-rs.query.api.dvb.corpinter.net"

RCP_BASE_URI_CN = "https://rcp-rs.query.api.dvb.corpinter.net.cn"

REST_API_BASE = "https://bff.emea-prod.mobilesdk.mercedes-benz.com"

REST_API_BASE_CN = "https://bff.cn-prod.mobilesdk.mercedes-benz.com"

REST_API_BASE_NA = "https://bff.amap-prod.mobilesdk.mercedes-benz.com"

REST_API_BASE_PA = "https://bff.amap-prod.mobilesdk.mercedes-benz.com"

WEBSOCKET_API_BASE = "wss://websocket.emea-prod.mobilesdk.mercedes-benz.com/v2/ws"

WEBSOCKET_API_BASE_NA = "wss://websocket.amap-prod.mobilesdk.mercedes-benz.com/v2/ws"

WEBSOCKET_API_BASE_CN = "wss://websocket.cn-prod.mobilesdk.mercedes-benz.com/v2/ws"

WEBSOCKET_USER_AGENT = "Mercedes-Benz/3044 CFNetwork/3860.400.22 Darwin/25.3.0"

WEBSOCKET_USER_AGENT_CN = "MyStarCN/1.63.0 (com.daimler.ris.mercedesme.cn.ios; build:1758; iOS 16.3.1) Alamofire/5.4.0"

DEFAULT_SOCKET_MIN_RETRY = 15

SIGPOS_HORN_TYPES = ("PANIC_ALARM", "LIGHT_AND_HORN", "HORN_ONLY")
