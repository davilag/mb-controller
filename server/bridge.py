"""Standalone lifecycle, normalized vehicle state, and an explicit command allowlist."""
from __future__ import annotations

import asyncio
from datetime import date, datetime
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from .mbapi.car import Car, CarAttribute
from .mbapi.client import Client
from .mbapi.errors import MBAuth2FAError, MBAuthError, MBLegalTermsError
from .mbapi.proto import client_pb2
from .runtime import ConfigEntry, HomeAssistant, TokenStore, async_get_clientsession


class BridgeError(Exception):
    pass


# UI label, upstream method, required capability. No arbitrary method dispatch.
COMMANDS = {
    "lock": ("Lock doors", "doors_lock", "DOORS_LOCK"),
    "unlock": ("Unlock doors", "doors_unlock_with_pin", "DOORS_UNLOCK"),
    "climate_start": ("Start climate", "preheat_start_universal", "ZEV_PRECONDITIONING_START"),
    "climate_stop": ("Stop climate", "preheat_stop", "ZEV_PRECONDITIONING_STOP"),
    "auxheat_start": ("Start auxiliary heating", "auxheat_start", "AUXHEAT_START"),
    "auxheat_stop": ("Stop auxiliary heating", "auxheat_stop", "AUXHEAT_STOP"),
    "flash": ("Flash lights", "sigpos_start", "SIGPOS_START"),
    "charge_limit": ("Set charge limit", "set_charge_limit", "BATTERY_MAX_SOC_CONFIGURE"),
}


class LocalClient(Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent_request = None

    async def execute_car_command(self, message):
        # Fail visibly when disconnected instead of returning a misleading success.
        socket = self.websocket._connection
        if not socket or socket.closed:
            raise BridgeError("Vehicle connection is offline. Wait for it to reconnect.")
        await super().execute_car_command(message)
        self.sent_request = message.commandRequest.request_id

    async def set_charge_limit(self, vin, max_soc):
        car = self.cars[vin]
        capability = charge_limit_capability(car)
        if capability == "CHARGING_CONFIGURE":
            await self.battery_max_soc_configure(vin, max_soc)
        elif capability == "CHARGE_PROGRAM_CONFIGURE":
            current = getattr(car.electric, "selectedChargeProgram", None)
            if not current or current.retrievalstatus != "VALID" or str(current.value) not in {"0", "2", "3"}:
                raise BridgeError("Waiting for the active charging program. Try again after vehicle data arrives.")
            # Configuring a different program would also activate it. Preserve the
            # vehicle's reported selection rather than defaulting to Standard.
            await self.charge_program_configure(vin, int(current.value), max_soc)
        elif capability == "BATTERY_MAX_SOC_CONFIGURE":
            message = client_pb2.ClientMessage()
            message.commandRequest.vin = vin
            message.commandRequest.request_id = str(uuid.uuid4())
            message.commandRequest.battery_max_soc.max_soc = max_soc
            await self.execute_car_command(message)
        else:
            raise BridgeError("Mercedes has not enabled charge-limit changes for this vehicle.")


def charge_limit_capability(car):
    for name in ("CHARGING_CONFIGURE", "CHARGE_PROGRAM_CONFIGURE", "BATTERY_MAX_SOC_CONFIGURE"):
        if not car.features.get(name):
            continue
        if name == "CHARGE_PROGRAM_CONFIGURE":
            command = next((c for c in (car.capabilities or {}).get("commands", []) if c.get("commandName") == name), {})
            if not any(p.get("parameterName") == "MAX_SOC" for p in command.get("parameters") or []):
                continue
        return name
    return None


def charge_limit_options(car):
    name = charge_limit_capability(car)
    if not name:
        return []
    command = next((c for c in (car.capabilities or {}).get("commands", []) if c.get("commandName") == name), {})
    parameter = next((p for p in command.get("parameters") or [] if p.get("parameterName") == "MAX_SOC"), {})
    minimum = parameter.get("minValue")
    maximum = parameter.get("maxValue")
    minimum = float(minimum) if minimum is not None else 50
    maximum = float(maximum) if maximum is not None else 100
    return [value for value in range(50, 101, 10) if minimum <= value <= maximum]


def plain(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def available_commands(car):
    result = []
    for name, (label, method, feature) in COMMANDS.items():
        available = bool(car.features.get(feature))
        if name == "climate_start":
            available = available or bool(car.features.get("precondNow"))
        if name == "charge_limit":
            available = bool(charge_limit_options(car))
        if name == "flash":
            # SIGPOS_START alone can mean horn-only. Respect advertised submodes.
            for command in (car.capabilities or {}).get("commands", []):
                if command.get("commandName") == "SIGPOS_START":
                    for param in command.get("parameters") or []:
                        if param.get("parameterName") == "SIGPOS_TYPE":
                            allowed = param.get("allowedEnums") or []
                            if allowed and "LIGHT_ONLY" not in allowed:
                                available = False
        item = {"id": name, "label": label, "available": available, "requiresPin": name == "unlock"}
        if name == "charge_limit":
            item["chargeLimits"] = charge_limit_options(car)
        result.append(item)
    return result


def serialize_car(car):
    attributes = {}
    for group in ("odometer", "doors", "electric", "tires", "windows", "precond", "auxheat", "binarysensors", "location"):
        obj = getattr(car, group, None)
        if obj is None:
            continue
        for key, attr in vars(obj).items():
            if isinstance(attr, CarAttribute):
                attributes[f"{group}.{key}"] = {
                    "value": plain(attr.value), "display": plain(attr.display_value),
                    "unit": attr.unit, "status": attr.retrievalstatus,
                    "updatedAt": plain(attr.timestamp),
                }
    return {
        "vin": car.finorvin,
        "name": car.baumuster_description or "Mercedes-Benz",
        "plate": car.licenseplate,
        "updatedAt": car._last_message_received or None,
        "attributes": attributes,
        "commands": available_commands(car),
        "lastCommand": {
            "type": car.last_command_type.value,
            "state": car.last_command_state.value,
            "error": car.last_command_error_message.value,
            "timestamp": car.last_command_time_stamp,
        },
    }


class Bridge:
    def __init__(self, directory: Path):
        self.directory = directory
        self.store = TokenStore(directory)
        self.runtime = None
        self.client = None
        self.task = None
        self.error = None
        self.operation_lock = asyncio.Lock()
        self.command_lock = asyncio.Lock()
        self.last_sent = 0

    async def create_client(self, data):
        self.runtime = HomeAssistant(self.directory, self.store)
        entry = ConfigEntry(data=data)
        self.client = LocalClient(self.runtime, async_get_clientsession(self.runtime), entry, region=data.get("region", "Europe"))
        self.runtime.data["mbapi2020"] = {"local": SimpleNamespace(client=self.client)}
        await self.client.set_rlock_mode()

    async def restore(self):
        async with self.operation_lock:
            try:
                data = self.store.load()
                if data.get("token"):
                    await self.create_client(data)
                    async with asyncio.timeout(100):
                        await self.discover()
            except Exception:
                self.error = "Could not restore your vehicle connection. Reconnect your account."
                await self.stop()

    async def connect(self, email, password, region):
        async with self.operation_lock:
            await self.stop()
            self.error = None
            await self.create_client({"username": email, "region": region})
            try:
                async with asyncio.timeout(100):
                    await self.client.oauth.async_login_new(email, password)
                    await self.discover()
            except MBAuth2FAError:
                await self.stop()
                raise BridgeError("This upstream login flow does not support accounts that require MFA. Use an eligible secondary vehicle account.") from None
            except MBLegalTermsError:
                await self.stop()
                raise BridgeError("Open the official Mercedes-Benz app and accept the current terms, then reconnect.") from None
            except MBAuthError:
                await self.stop()
                raise BridgeError("Mercedes login failed. Check your account details and sign in to the official app first.") from None
            except Exception as error:
                await self.stop()
                if isinstance(error, BridgeError):
                    raise
                raise BridgeError("Could not connect to Mercedes. Check connectivity and your account, then try again.") from None

    async def discover(self):
        client = self.client
        if not await client.oauth.async_get_cached_token():
            raise BridgeError("Your session expired. Reconnect your account.")
        config, master = await asyncio.gather(client.webapi.get_config(), client.webapi.get_user_info())
        if not isinstance(master, dict):
            raise BridgeError("Mercedes did not return vehicle information.")
        vehicles = list(master.get("assignedVehicles") or [])
        for fleet in master.get("fleets") or []:
            info = await client.webapi.get_fleet_info(fleet.get("companyId"), fleet.get("fleetId"))
            vehicles.extend((info or {}).get("bookedVehicles") or [])
        for item in vehicles:
            vin = item.get("vin") or item.get("fin")
            if not vin:
                continue
            car = Car(vin)
            car.licenseplate = item.get("licensePlate") or vin
            sales = item.get("salesRelatedInformation") or {}
            car.baumuster_description = (sales.get("baumuster") or {}).get("baumusterDescription") or ""
            car.masterdata = item
            car.app_configuration = config or {}
            car.is_owner = item.get("isOwner", False)
            responses = await asyncio.gather(
                client.webapi.get_car_capabilities(vin), client.webapi.get_car_capabilities_commands(vin),
                return_exceptions=True,
            )
            details, capabilities = responses
            if isinstance(details, dict):
                car.features.update(details.get("features") or {})
                car.vehicle_information = details.get("vehicle") or {}
            if isinstance(capabilities, dict):
                car.capabilities = capabilities
                for feature in capabilities.get("commands") or []:
                    car.features[feature.get("commandName")] = bool(feature.get("isAvailable"))
            client.cars[vin] = car
        if not client.cars:
            raise BridgeError("No vehicles were returned. Check that this account has access to your Mercedes.")
        self.task = asyncio.create_task(self.run_socket())

    async def run_socket(self):
        async def on_ready():
            pass
        try:
            await self.client.attempt_connect(on_ready)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.error = "Vehicle connection stopped. Reconnect your account."

    def state(self):
        client = self.client
        socket = client.websocket if client else None
        connected = bool(socket and socket._connection and not socket._connection.closed)
        return {
            "configured": bool(client and client.cars), "connected": connected,
            "connection": socket.connection_state if socket else "disconnected",
            "error": self.error or ("Reconnect required." if self.runtime and self.runtime.reload_requested else None),
            "region": client._region if client else "Europe",
            "vehicles": [serialize_car(car) for car in client.cars.values()] if client else [],
        }

    async def command(self, vin, name, pin="", max_soc=None):
        async with self.command_lock:
            if self.operation_lock.locked():
                raise BridgeError("Account connection is changing. Try again when it finishes.")
            client = self.client
            if not client or vin not in client.cars:
                raise BridgeError("Select a vehicle connected to your account.")
            if name not in COMMANDS:
                raise BridgeError("Unknown command.")
            if not any(c["id"] == name and c["available"] for c in available_commands(client.cars[vin])):
                raise BridgeError("Mercedes has not enabled this command for your vehicle.")
            if name == "unlock" and (not pin.isdigit() or len(pin) != 4):
                raise BridgeError("Enter the four-digit vehicle security PIN set in the official app.")
            if name == "charge_limit" and (type(max_soc) is not int or max_soc not in charge_limit_options(client.cars[vin])):
                raise BridgeError("Select a supported charge limit from the available percentages.")
            if time.monotonic() - self.last_sent < 5:
                raise BridgeError("Wait a few seconds before sending another command.")
            client.sent_request = None
            method = getattr(client, COMMANDS[name][1])
            async with asyncio.timeout(20):
                if name == "unlock":
                    await method(vin, pin)
                elif name == "charge_limit":
                    await method(vin, max_soc)
                else:
                    await method(vin)
            if not client.sent_request:
                raise BridgeError("The integration did not send this command. Check vehicle capabilities.")
            self.last_sent = time.monotonic()
            return {"requestId": client.sent_request, "state": "sent", "message": "Command sent. Waiting for Mercedes confirmation; this does not confirm completion."}

    async def stop(self):
        if self.client:
            self.client._cancel_dataload_complete_fallback()
            socket = self.client.websocket
            socket._unloaded = True
            socket.component_reload_watcher.cancel()
            await socket.async_stop()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.runtime:
            await self.runtime.close()
        self.client = None
        self.runtime = None

    async def disconnect(self):
        async with self.operation_lock:
            async with self.command_lock:
                await self.stop()
                self.store.clear()
                self.error = None
