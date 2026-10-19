"""Request and response models for the system inventory."""

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from pi.core.models import Strict


class Read(Strict):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,100}$")
    limit: int = Field(default=100, ge=1, le=200)


ErrorCode = Literal[
    "process_identity_unavailable",
    "process_changed_during_collection",
    "process_unavailable",
    "collection_failed",
    "process_link_unavailable",
    "listener_collection_failed",
    "container_identity_unavailable",
    "invalid_container_binding",
    "container_unavailable",
    "container_bindings_unavailable",
    "container_telemetry_not_configured",
    "container_bindings_not_configured",
    "client_close_failed",
]


class ObservedProcess(Strict):
    id: str = Field(max_length=256)
    pid: int = Field(gt=0)
    createdAt: float = Field(ge=0, allow_inf_nan=False)
    name: str = Field(max_length=160)
    status: str = Field(max_length=40)
    memoryBytes: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cpuPercent: None
    command: None
    user: None
    restarts: None
    containerId: None
    managed: Literal[False]

    @model_validator(mode="after")
    def identity(self):
        if self.id != f"process:{self.pid}:{float(self.createdAt).hex()}":
            raise ValueError("Invalid process identity.")
        return self


class ObservedContainer(Strict):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(max_length=160)
    image: str = Field(max_length=256)
    status: str = Field(max_length=40)
    processId: None
    restarts: None
    managed: Literal[False]


class ObservedPort(Strict):
    id: str = Field(pattern=r"^(?:listener|binding):[a-f0-9]{64}$")
    kind: Literal["listener", "container-binding"]
    hostAddress: str = Field(max_length=64)
    hostPort: int = Field(ge=1, le=65535)
    targetPort: int | None = Field(default=None, ge=1, le=65535)
    protocol: Literal["tcp", "udp"]
    processId: str | None = Field(default=None, max_length=256)
    containerId: str | None = Field(default=None, max_length=128)
    listening: bool | None
    bound: bool | None

    @model_validator(mode="after")
    def semantics(self):
        if self.kind == "container-binding":
            valid = (
                self.id.startswith("binding:")
                and self.containerId is not None
                and self.targetPort is not None
                and self.processId is None
                and self.listening is None
                and self.bound is None
            )
        else:
            valid = (
                self.id.startswith("listener:")
                and self.containerId is None
                and self.targetPort is None
                and self.bound is True
                and self.listening is (True if self.protocol == "tcp" else None)
            )
        if not valid:
            raise ValueError("Invalid port observation.")
        return self


class ObservedSection(Strict):
    status: Literal["ok", "partial", "unavailable"]
    truncated: bool
    errors: list[ErrorCode] = Field(max_length=16)


class ObservedProcesses(ObservedSection):
    results: list[ObservedProcess] = Field(max_length=200)


class ObservedContainers(ObservedSection):
    results: list[ObservedContainer] = Field(max_length=200)


class ObservedPorts(ObservedSection):
    results: list[ObservedPort] = Field(max_length=200)


class ObservedSource(Strict):
    procfs: str = Field(max_length=4096)
    processScope: Literal["configured-procfs", "collector-namespace"]
    networkScope: Literal["collector-namespace"]
    containerScope: Literal[
        "configured-docker-daemon", "configured-container-source", "unavailable"
    ]


class ObservedCapabilities(Strict):
    inspection: Literal[True]
    processActions: Literal[False]
    containerActions: Literal[False]
    portMutation: Literal[False]
    terminal: Literal[False]
    files: Literal[False]

    @field_validator("*", mode="before")
    @classmethod
    def strict_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("Capabilities must use strict booleans.")
        return value


class ObservedEnvelope(Strict):
    mode: Literal["observed"]
    sampledAt: str = Field(max_length=64)
    ageSeconds: float = Field(ge=0, allow_inf_nan=False)
    collectionSeconds: float = Field(ge=0, allow_inf_nan=False)
    source: ObservedSource
    status: Literal["ok", "partial"]
    processes: ObservedProcesses
    containers: ObservedContainers
    ports: ObservedPorts
    capabilities: ObservedCapabilities
    unavailableFields: list[str] = Field(max_length=32)

    @field_validator("unavailableFields")
    @classmethod
    def unavailable_fields(cls, values):
        if any(len(value) > 256 for value in values):
            raise ValueError("Unavailable field labels are too long.")
        return values

    @model_validator(mode="after")
    def consistency(self):
        sampled = datetime.fromisoformat(self.sampledAt)
        if sampled.tzinfo is None:
            raise ValueError("Timestamp lacks timezone.")
        sections = (self.processes, self.containers, self.ports)
        expected = "partial" if any(s.status != "ok" or s.truncated for s in sections) else "ok"
        if self.status != expected:
            raise ValueError("Inventory status does not match sections.")
        for section in sections:
            if (section.status == "ok" and section.errors) or (
                section.status == "unavailable" and section.results
            ):
                raise ValueError("Inventory section is inconsistent.")
            ids = [row.id for row in section.results]
            if len(ids) != len(set(ids)):
                raise ValueError("Inventory identities must be unique.")
        processes = {row.id for row in self.processes.results}
        containers = {row.id for row in self.containers.results}
        if any(
            (row.processId is not None and row.processId not in processes)
            or (row.containerId is not None and row.containerId not in containers)
            for row in self.ports.results
        ):
            raise ValueError("Inventory references must resolve within the sample.")
        return self


class ProcessView(Strict):
    id: str = Field(pattern=r"^process_[a-f0-9]{64}$")
    name: str = Field(max_length=160)
    status: str = Field(max_length=40)
    createdAt: float = Field(ge=0, allow_inf_nan=False)
    memoryBytes: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ContainerView(Strict):
    id: str = Field(pattern=r"^container_[a-f0-9]{64}$")
    name: str = Field(max_length=160)
    status: str = Field(max_length=40)
    imageConfigured: bool


class PortView(Strict):
    id: str = Field(pattern=r"^port_[a-f0-9]{64}$")
    kind: Literal["listener", "container-binding"]
    addressScope: Literal["loopback", "all-interfaces", "specific"]
    hostPort: int = Field(ge=1, le=65535)
    targetPort: int | None = Field(default=None, ge=1, le=65535)
    protocol: Literal["tcp", "udp"]
    processId: str | None = Field(default=None, pattern=r"^process_[a-f0-9]{64}$")
    containerId: str | None = Field(default=None, pattern=r"^container_[a-f0-9]{64}$")
    state: Literal["listening", "bound", "declared-binding"]


class SectionView(Strict):
    status: Literal["ok", "partial", "unavailable"]
    truncated: bool
    errors: list[ErrorCode] = Field(max_length=16)


class ProcessSectionView(SectionView):
    results: list[ProcessView] = Field(max_length=200)


class ContainerSectionView(SectionView):
    results: list[ContainerView] = Field(max_length=200)


class PortSectionView(SectionView):
    results: list[PortView] = Field(max_length=200)


class SourceScopeView(Strict):
    process: Literal["configured-procfs", "collector-namespace"]
    network: Literal["collector-namespace"]
    containers: Literal["configured-docker-daemon", "configured-container-source", "unavailable"]


class InventoryObservation(Strict):
    mode: Literal["observed"]
    status: Literal["ok", "partial"]
    sampledAt: str = Field(max_length=64)
    ageSeconds: float = Field(ge=0, allow_inf_nan=False)
    collectionSeconds: float = Field(ge=0, allow_inf_nan=False)
    sourceScopes: SourceScopeView
    processes: ProcessSectionView
    containers: ContainerSectionView
    ports: PortSectionView
    unavailableFieldCount: int = Field(ge=0, le=32)
    capabilities: ObservedCapabilities


class InventoryView(Strict):
    schemaVersion: Literal[1] = 1
    requestId: str = Field(pattern=r"^[A-Za-z0-9_-]{16,100}$")
    state: Literal["dispatching", "awaiting_approval", "unknown", "failed", "complete"]
    limit: int = Field(ge=1, le=200)
    approvalRequired: bool
    errorCode: Literal["invalid_inventory", "read_failed"] | None
    observation: InventoryObservation | None
    receiptStatus: Literal["unavailable"] | None = None
    currentAgeSeconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    createdAt: float = Field(ge=0, allow_inf_nan=False)
    updatedAt: float = Field(ge=0, allow_inf_nan=False)
    source: Literal["toolgate/system.inventory"]
    refreshRequiresNewRequest: Literal[True]
    authority: Literal["none"] = "none"
    contentIncluded: bool
    execution: Literal["read-only-observation"] = "read-only-observation"


class ConfiguredTarget(Strict):
    id: str = Field(pattern=r"^(?:service|container)_[a-f0-9]{64}$")
    kind: Literal["service", "container"]
    name: str | None = Field(default=None, max_length=260)


class ConfiguredTargetsView(Strict):
    schemaVersion: Literal[1] = 1
    kind: Literal["services", "containers"]
    status: Literal[
        "configured", "disabled", "locked_down", "not_configured", "invalid_configuration"
    ]
    results: list[ConfiguredTarget] = Field(max_length=2000)
    requiresApproval: Literal[True]
    observed: Literal[False]
    authority: Literal["none"] = "none"
    contentIncluded: bool
    execution: Literal["not-triggered"] = "not-triggered"
