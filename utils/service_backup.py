"""Selected-service orchestration using the installation archive/recovery lane.

Schema 2 deliberately cannot be read as a complete installation by an older
controller. Application roots and logical databases are independent of global
controller settings, authentication and the shared PostgreSQL data directory.
"""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

from utils.backup_storage import paths_overlap, require_local_path
from utils.installation_backup_archive import (
    FORMAT,
    InstallationArchiveError,
    canonical,
    identity,
)
from utils.installation_backup_inventory import (
    capacity,
    compatibility,
    ensure_local_restore_roots,
)
from utils.installation_backup_limits import MAX_INSTALLATION_ROOTS
from utils.service_backup_scope import (
    affected_running_rows,
    configured_service_entries,
    enumerate_service_choices,
    merge_service_config,
    resolve_service_scope,
)
from utils.service_backup_postgres import PostgresServiceBackup, service_databases

SERVICE_SCHEMA = 2
CONFIG_NAME = "selected-configuration.json"


def scope_options(options, *, allow_empty=False):
    """Validate direct, scheduled and preview callers, including non-HTTP callers."""
    kind = options.get("scope", "installation")
    ids = options.get("service_ids", [])
    if (
        not isinstance(kind, str)
        or kind not in {"installation", "services"}
        or not isinstance(ids, list)
        or len(ids) > 64
        or any(not isinstance(item, str) or not 1 <= len(item) <= 128 for item in ids)
        or len(ids) != len(set(ids))
        or (kind == "installation" and ids)
        or (kind == "services" and not ids and not allow_empty)
    ):
        raise InstallationArchiveError(
            "Select a valid backup scope and exact service instances."
        )
    return {"scope": kind, "service_ids": sorted(ids)}


def is_service_plan(plan):
    return (plan.get("service_scope") or {}).get("kind") == "services"


def retention_key(receipt):
    scope = receipt.get("service_scope")
    if scope is None or scope == {"kind": "installation"}:
        return ("installation",)
    if not isinstance(scope, dict) or scope.get("kind") != "services":
        return None
    ids = scope.get("service_ids")
    if (
        not isinstance(ids, list)
        or not ids
        or any(not isinstance(item, str) for item in ids)
    ):
        return None
    return ("services", *sorted(set(ids)))


def public_scope(scope):
    return {
        "kind": "services",
        "service_ids": deepcopy(scope["service_ids"]),
        "services": [
            {key: row[key] for key in ("id", "key", "instance_name", "name")}
            for row in scope["services"]
        ],
        "affected_process_names": list(scope.get("affected_process_names", [])),
        "database_count": scope.get("database_count", 0),
    }


def proof_only(proof):
    return {
        key: value
        for key, value in proof.items()
        if key not in {"metadata", "service_configuration"}
    }


class ServiceBackupOperations:
    def __init__(self, manager):
        self.manager = manager

    def resolve(self, ids, configuration=None, *, restore_roots=()):
        manager = self.manager
        return resolve_service_scope(
            configuration if configuration is not None else manager.config,
            ids,
            manager.config_manager.file_path,
            manager.state_root,
            manager.storage,
            restore_roots=restore_roots,
        )

    @staticmethod
    def database_sets(configuration, scope):
        result = []
        for service in scope["services"]:
            specs = service_databases(
                configuration, service["key"], service["instance_name"]
            )
            if specs:
                result.append({"service_id": service["id"], "specs": specs})
        names = [item["database"] for row in result for item in row["specs"]]
        if len(names) != len(set(names)):
            raise InstallationArchiveError(
                "The selected services share a database; use a whole-installation backup."
            )
        return result

    def choices(self):
        manager = self.manager
        choices = enumerate_service_choices(
            manager.config, manager.config_manager.file_path, manager.state_root
        )
        for row in choices:
            if row["supported"]:
                try:
                    service_databases(manager.config, row["key"], row["instance_name"])
                except InstallationArchiveError as error:
                    row.update(supported=False, reason=str(error))
            row["available"] = row.pop("supported")
            reason = row.pop("reason")
            row["blocking_reasons"] = [reason] if reason else []
        return {"protocol": 1, "services": choices}

    def describe(self, scope):
        scope = deepcopy(scope)
        manager = self.manager
        if manager.handler is not None:
            rows = affected_running_rows(
                manager.config, scope["service_ids"], manager._running()
            )
            scope["affected_process_names"] = [row["name"] for row in rows]
        scope["database_count"] = sum(
            len(row["specs"]) for row in self.database_sets(manager.config, scope)
        )
        return public_scope(scope)

    def _artifacts(self, scratch, scope):
        root = scratch / "service-artifacts"
        # Previews are serialized; old preview artifacts are ours, never source state.
        if root.exists():
            if root.is_symlink() or root.stat().st_uid != os.geteuid():
                raise InstallationArchiveError(
                    "The selected-service workspace changed ownership."
                )
            shutil.rmtree(root)
        root.mkdir(mode=0o700)
        self.manager._write(root / CONFIG_NAME, scope["configuration"])
        return {
            "id": f"root_{len(scope['roots']):03d}",
            "path": str(root),
            "identity": identity(root.lstat()),
            "kind": "directory",
            "roles": ["service_artifacts"],
            "link_only": False,
            "service_internal": True,
        }

    @staticmethod
    def _journal_budget(roots):
        budget = 0
        for root in roots:
            if root["kind"] != "directory" or not os.path.lexists(root["path"]):
                continue
            with os.scandir(root["path"]) as entries:
                for count, entry in enumerate(entries, 1):
                    if count > 8192:
                        raise InstallationArchiveError(
                            "A service root exceeds the bounded restore transaction budget."
                        )
                    budget += 2 * (len(canonical(entry.name)) + 128)
            budget += len(canonical(root)) + 1024
        if budget > 1024 * 1024:
            raise InstallationArchiveError(
                "The selected service restore journal would exceed its recovery bound."
            )

    def backup_plan(self, options):
        manager = self.manager
        scope = self.resolve(scope_options(options)["service_ids"])
        destination = manager.storage.bound_identity(options.get("destination_id"))
        scratch = manager._workspace("preview")
        database_sets = self.database_sets(manager.config, scope)
        database_bytes = 0
        if database_sets:
            adapter = PostgresServiceBackup(manager.config)
            database_bytes = sum(
                adapter.estimate(row["specs"]) for row in database_sets
            )
        scope["affected_process_names"] = self.describe(scope)["affected_process_names"]
        scope["database_count"] = sum(len(row["specs"]) for row in database_sets)
        artifact = self._artifacts(scratch, scope)
        roots = [*scope["roots"], artifact]
        if len(roots) > MAX_INSTALLATION_ROOTS:
            raise InstallationArchiveError(
                "The service bundle exceeds the archive root bound."
            )
        self._journal_budget(scope["roots"])
        exclusions = [row["path"] for row in manager._retained_holders()]
        for root in scope["roots"]:
            if paths_overlap(
                destination["artifact_root"],
                root["path"],
                second_leaf_object=root["kind"] in {"file", "symlink"},
            ):
                raise InstallationArchiveError(
                    "The selected archive store overlaps this service's persistence."
                )
        return {
            "service_scope": public_scope(scope),
            "service_configuration": scope["configuration"],
            "database_sets": database_sets,
            "database_estimated_bytes": database_bytes,
            "roots": roots,
            "exclusions": exclusions,
            "destination": destination,
            "compatibility": compatibility(manager.config),
            "compression": options.get("compression", "none"),
        }, scratch

    @staticmethod
    def reserve_database_capacity(report, amount):
        """Conservative preview allowance for dump, verification and archive copies."""
        if not amount:
            return report
        report = deepcopy(report)
        for filesystem in report["filesystems"]:
            filesystem["base_required_bytes"] += amount * 3
            base = filesystem["base_required_bytes"]
            filesystem["safety_margin_bytes"] = (base + 9) // 10
            filesystem["required_bytes"] = base + filesystem["safety_margin_bytes"]
            filesystem["shortfall_bytes"] = max(
                0, filesystem["required_bytes"] - filesystem["free_bytes"]
            )
            filesystem["sufficient"] = filesystem["shortfall_bytes"] == 0
        report["status"] = (
            "sufficient"
            if all(row["sufficient"] for row in report["filesystems"])
            else "insufficient"
        )
        report["database_estimated_bytes"] = amount
        return report

    def prepare_backup(self, identifier, plan):
        manager = self.manager
        current = self.resolve(plan["service_scope"]["service_ids"])
        if current["configuration"] != plan["service_configuration"]:
            raise InstallationArchiveError(
                "Selected service configuration changed before backup."
            )
        expected = [row for row in plan["roots"] if not row.get("service_internal")]
        if current["roots"] != expected:
            raise InstallationArchiveError(
                "Selected service persistence changed before backup."
            )
        current["database_count"] = plan["service_scope"]["database_count"]
        scratch = manager._workspace(identifier)
        artifact = self._artifacts(scratch, current)
        databases = []
        if plan["database_sets"]:
            adapter = PostgresServiceBackup(manager.config)
            estimated = sum(
                adapter.estimate(row["specs"]) for row in plan["database_sets"]
            )
            plan = deepcopy(plan)
            plan["database_estimated_bytes"] = max(
                estimated, plan["database_estimated_bytes"]
            )
            measured = {"entries": 0, "logical_bytes": 0}
            archive_progress = manager._backup_progress(identifier)

            def scan_progress(**values):
                manager._owner_check(identifier)
                measured.update({key: values[key] for key in measured if key in values})
                archive_progress(
                    stage="inventorying", force=values.get("force", False), **measured
                )

            report, _ = capacity(
                [*current["roots"], artifact],
                scratch,
                plan["destination"]["path"],
                plan["exclusions"],
                compression=plan.get("compression", "none"),
                progress=scan_progress,
            )
            report = self.reserve_database_capacity(
                report, plan["database_estimated_bytes"]
            )
            manager._progress(
                identifier,
                "inventorying",
                "Checking stopped-state space for selected database exports and the complete service archive before any database dump is written.",
                capacity=report,
            )
            if report["status"] != "sufficient":
                raise InstallationArchiveError(
                    "Insufficient space for selected database exports and archive verification; no database dump or archive payload was written."
                )
            database_root = Path(artifact["path"]) / "logical-postgres"
            database_root.mkdir(mode=0o700)
            for index, selected in enumerate(plan["database_sets"]):
                manager._check_backup_cancel(identifier)
                folder = database_root / f"{index:03d}"
                folder.mkdir(mode=0o700)
                manager._progress(
                    identifier,
                    "database_backup",
                    "Exporting the selected service databases; shared PostgreSQL stays online.",
                    database_progress=None,
                )
                manifest = adapter.backup(
                    selected["specs"],
                    folder,
                    progress=self.database_progress(identifier, backup=True),
                )
                databases.append(
                    {
                        "service_id": selected["service_id"],
                        "directory": f"logical-postgres/{index:03d}",
                        "manifest": manifest,
                    }
                )
        plan = deepcopy(plan)
        plan["roots"] = [*current["roots"], artifact]
        manager._progress(
            identifier,
            "inventorying",
            "Measuring the stopped services and their logical database exports.",
            plan=plan,
        )
        metadata = {
            "format": FORMAT,
            "schema_version": SERVICE_SCHEMA,
            "created_at": manager.clock(),
            "compatibility": plan["compatibility"],
            "roots": plan["roots"],
            "excluded_paths": plan["exclusions"],
            "service_scope": plan["service_scope"],
            "service_artifacts_root_id": artifact["id"],
            "service_databases": databases,
            "database_estimated_bytes": plan["database_estimated_bytes"],
            "config_path": str(Path(artifact["path"]) / CONFIG_NAME),
            "config_sha256": manager._controller_json(
                Path(artifact["path"]) / CONFIG_NAME
            ),
            "scope": {
                "includes": [
                    "Selected application settings and exclusive persistence",
                    "Selected logical PostgreSQL databases",
                ],
                "excludes": [
                    "DUMB accounts and global settings",
                    "Unselected services",
                    "Shared PostgreSQL cluster files",
                    "External media and external services",
                ],
            },
        }
        return plan, metadata

    def database_progress(self, identifier, *, backup=False):
        last = [0.0, None]

        def report(value):
            manager = self.manager
            manager._owner_check(identifier)
            if backup:
                manager._check_backup_cancel(identifier)
            now = time.monotonic()
            phase = value.get("phase")
            if (
                now - last[0] < 0.5
                and last[1] == phase
                and value.get("completed") != value.get("total")
            ):
                return
            last[:] = [now, phase]
            manager._progress(
                identifier,
                "database_backup" if backup else "database_restore",
                (
                    "Exporting and checking selected databases."
                    if backup
                    else "Restoring and checking selected databases in isolation."
                ),
                database_progress=value,
            )

        return report

    def _restore_selection(self, metadata, configuration):
        if metadata.get("schema_version") != SERVICE_SCHEMA:
            raise InstallationArchiveError(
                "This archive is not a selected-service backup."
            )
        scope = metadata.get("service_scope")
        if not isinstance(scope, dict) or scope.get("kind") != "services":
            raise InstallationArchiveError("The service archive selection is invalid.")
        ids = scope_options(
            {"scope": "services", "service_ids": scope.get("service_ids")}
        )["service_ids"]
        artifact_id = metadata.get("service_artifacts_root_id")
        roots = metadata.get("roots", [])
        artifacts = [row for row in roots if row.get("id") == artifact_id]
        application_roots = [row for row in roots if row.get("id") != artifact_id]
        current = self.resolve(ids, restore_roots=application_roots)
        merged = merge_service_config(
            self.manager.config,
            configuration,
            ids,
            schema=getattr(self.manager.config_manager, "schema", None),
        )
        restored = self.resolve(ids, merged, restore_roots=application_roots)
        current_entries = {
            row["id"]: row for row in configured_service_entries(self.manager.config)
        }
        new_entries = {row["id"]: row for row in configured_service_entries(merged)}
        for service_id in ids:
            if current_entries[service_id]["name"] != new_entries[service_id]["name"]:
                raise InstallationArchiveError(
                    "Service restore requires the same configured instance and process names."
                )
            # Restore settings without changing the operator's current enabled selection.
            new_entries[service_id]["service_config"]["enabled"] = current_entries[
                service_id
            ]["service_config"].get("enabled", False)
        fields = ("path", "kind", "link_only", "service_ids")
        signature = lambda rows: sorted(
            canonical({key: row.get(key) for key in fields}) for row in rows
        )
        if (
            len(artifacts) != 1
            or artifacts[0].get("kind") != "directory"
            or artifacts[0].get("service_internal") is not True
            or artifacts[0].get("roles") != ["service_artifacts"]
            or metadata.get("config_path")
            != str(Path(artifacts[0]["path"]) / CONFIG_NAME)
            or signature(application_roots) != signature(current["roots"])
            or signature(restored["roots"]) != signature(current["roots"])
        ):
            raise InstallationArchiveError(
                "The archive does not match the selected services' current exclusive container paths."
            )
        expected_sets = self.database_sets(self.manager.config, current)
        if self.database_sets(merged, restored) != expected_sets:
            raise InstallationArchiveError(
                "Restore cannot change a service's shared PostgreSQL database selection."
            )
        databases = metadata.get("service_databases", [])
        if not isinstance(databases, list) or len(databases) != len(expected_sets):
            raise InstallationArchiveError(
                "The selected-service database inventory is incomplete."
            )
        for index, (row, expected) in enumerate(zip(databases, expected_sets)):
            if (
                not isinstance(row, dict)
                or row.get("service_id") != expected["service_id"]
                or row.get("directory") != f"logical-postgres/{index:03d}"
                or [
                    {"database": item.get("database"), "role": item.get("role")}
                    for item in PostgresServiceBackup._manifest(row.get("manifest"))[
                        "databases"
                    ]
                ]
                != expected["specs"]
            ):
                raise InstallationArchiveError(
                    "The service database manifest differs from the current managed database selection."
                )
        if databases:
            adapter = PostgresServiceBackup(self.manager.config)
            for row in expected_sets:
                adapter.estimate(row["specs"])
        current["database_count"] = sum(len(row["specs"]) for row in expected_sets)
        return current, merged, application_roots

    def restore_plan(self, source, proof, scratch):
        from utils.installation_backup import _forbidden, _mount_binding

        manager = self.manager
        metadata = proof["metadata"]
        manager._verify_controller_proof(metadata, proof)
        if metadata.get("compatibility") != compatibility(manager.config):
            raise InstallationArchiveError(
                "Use the same controller/image, architecture, UID/GID and PostgreSQL major as the service backup."
            )
        scope, merged, roots = self._restore_selection(
            metadata, proof.get("service_configuration")
        )
        roots = deepcopy(roots)
        for row in roots:
            if _forbidden(Path(row["path"])):
                raise InstallationArchiveError(
                    "A selected service restore target is a protected system path."
                )
            if row["kind"] != "directory" and not Path(row["path"]).parent.is_dir():
                raise InstallationArchiveError(
                    "Recreate the selected file's configured parent directory before restore; container paths cannot be remapped."
                )
            row.pop("atomic_restore", None)
            row.pop("atomic_names", None)
        # The only controller target is an atomic merge of exact selected sections.
        config_path = Path(manager.config_manager.file_path)
        manager._controller_json(config_path)
        used = {row["id"] for row in metadata["roots"]}
        config_id = next(
            f"root_{index:03d}"
            for index in range(MAX_INSTALLATION_ROOTS + 1)
            if f"root_{index:03d}" not in used
        )
        config_root = {
            "id": config_id,
            "path": str(config_path),
            "identity": identity(config_path.lstat()),
            "kind": "file",
            "roles": ["selected_configuration_merge"],
            "atomic_restore": True,
        }
        roots.append(config_root)
        ensure_local_restore_roots(roots)
        self._journal_budget(roots)
        bindings = {
            row["id"]: _mount_binding(
                Path(row["path"]).parent if row["kind"] != "directory" else row["path"]
            )
            for row in roots
        }
        protected = [
            str(manager.state_root),
            *[row["path"] for row in manager._retained_holders()],
        ]
        protected.extend(
            profile["path"]
            for profile in (manager.config.get("backup_storage") or {})
            .get("profiles", {})
            .values()
            if isinstance(profile, dict) and isinstance(profile.get("path"), str)
        )
        if source.get("archive_path"):
            protected.append(source["archive_path"])
        for root in roots:
            for raw in protected:
                if (
                    paths_overlap(
                        root["path"],
                        raw,
                        first_leaf_object=root["kind"] in {"file", "symlink"},
                    )
                    and Path(root["path"]) != Path(raw).parent
                ):
                    # Retained top-level holders are explicitly protected by the transaction layer.
                    raise InstallationArchiveError(
                        "A selected restore root overlaps retained archive or controller state."
                    )
        capacity_proof = proof_only(proof)
        capacity_proof["root_sizes"] = dict(
            capacity_proof["root_sizes"], **{config_id: len(canonical(merged))}
        )
        scope = self.describe(scope)
        return {
            "source": source,
            "metadata": metadata,
            "proof": proof_only(proof),
            "capacity_proof": capacity_proof,
            "service_scope": scope,
            "service_configuration": proof["service_configuration"],
            "roots": roots,
            "bindings": bindings,
            "protected": protected,
            "config_root_id": config_id,
        }

    def restore_capacity(self, plan, scratch):
        manager = self.manager
        report = manager._restore_capacity(
            plan["capacity_proof"], plan["roots"], scratch
        )
        # Database size is conservatively reserved on each participating filesystem;
        # PostgreSQL can expand a compressed logical dump substantially.
        amount = plan["metadata"].get("database_estimated_bytes", 0)
        if type(amount) is not int or amount < 0:
            raise InstallationArchiveError(
                "The service database capacity inventory is invalid."
            )
        if plan["metadata"].get("service_databases"):
            amount = max(
                amount,
                sum(
                    item["bytes"]
                    for database in plan["metadata"]["service_databases"]
                    for item in database["manifest"]["databases"]
                ),
            )
            report = self.reserve_database_capacity(report, amount)
            raw = (manager.config.get("postgres") or {}).get(
                "config_dir"
            ) or "/postgres_data"
            require_local_path(raw)
            free = shutil.disk_usage(raw).free
            required = amount * 3 + (amount * 3 + 9) // 10
            report["filesystems"].append(
                {
                    "id": "service_postgres",
                    "roles": ["logical_database_restore"],
                    "base_required_bytes": amount * 3,
                    "safety_margin_bytes": (amount * 3 + 9) // 10,
                    "required_bytes": required,
                    "free_bytes": free,
                    "shortfall_bytes": max(0, required - free),
                    "sufficient": free >= required,
                }
            )
            report["status"] = (
                "sufficient"
                if all(row["sufficient"] for row in report["filesystems"])
                else "insufficient"
            )
        return report

    def _persist_database(self, identifier, index, transaction):
        manager = self.manager
        with manager._lock:
            job = manager._job(identifier)
            rows = job.setdefault("service_database_journal", [])
            if index == len(rows):
                rows.append(deepcopy(transaction))
            else:
                rows[index] = deepcopy(transaction)
            job["mutated"] = True
            manager._save(job)

    def restore(self, identifier):
        from utils.installation_backup import _mount_binding
        from utils.installation_backup_restore import prepare, apply

        manager = self.manager
        job = manager._job(identifier)
        plan = job["plan"]
        scratch = manager._workspace(identifier)
        if self.restore_capacity(plan, scratch)["status"] != "sufficient":
            raise InstallationArchiveError(
                "Service restore capacity changed before staging."
            )
        manager._progress(
            identifier,
            "verifying",
            "Verifying and staging the complete selected-service archive before stopping apps.",
        )
        stage = scratch / "verified-stage"
        stage.mkdir(mode=0o700)
        proof = manager._inspect_source(
            plan["source"],
            scratch,
            extract_root=stage,
            owner_check=lambda: manager._owner_check(identifier),
        )
        if (
            proof_only(proof) != plan["proof"]
            or proof["metadata"] != plan["metadata"]
            or proof.get("service_configuration") != plan["service_configuration"]
        ):
            raise InstallationArchiveError("The service archive changed after preview.")
        scope, merged, _ = self._restore_selection(
            proof["metadata"], proof["service_configuration"]
        )
        if manager._binding() != job["config_binding"]:
            raise InstallationArchiveError(
                "Managed configuration changed before service restore."
            )
        manager._write(stage / plan["config_root_id"], merged)
        manager._quiesce(identifier)
        manager._progress(
            identifier,
            "replacing",
            "Preparing selected app replacements with retained original files and databases.",
            rollback_journal=[],
            service_database_journal=[],
        )
        adapter = (
            PostgresServiceBackup(manager.config)
            if plan["metadata"].get("service_databases")
            else None
        )
        for index, row in enumerate(plan["metadata"].get("service_databases", [])):
            manager._progress(
                identifier,
                "database_restore",
                "Preparing an isolated selected-service database restore.",
                database_progress=None,
            )
            specs = next(
                item["specs"]
                for item in self.database_sets(manager.config, scope)
                if item["service_id"] == row["service_id"]
            )
            adapter.prepare_restore(
                row["manifest"],
                stage
                / plan["metadata"]["service_artifacts_root_id"]
                / row["directory"],
                identifier,
                lambda transaction, index=index: self._persist_database(
                    identifier, index, transaction
                ),
                progress=self.database_progress(identifier),
                specs=specs,
            )
        manager._progress(
            identifier,
            "replacing",
            "Replacing only the selected app roots and selected configuration sections.",
            mutated=True,
        )
        for root in plan["roots"]:
            anchor = (
                Path(root["path"]).parent
                if root["kind"] != "directory"
                else Path(root["path"])
            )
            if _mount_binding(anchor) != plan["bindings"][root["id"]]:
                raise InstallationArchiveError(
                    "A selected service restore filesystem changed."
                )
            index = len(manager._job(identifier)["rollback_journal"])
            persist = lambda row, index=index: manager._persist_transaction(
                identifier, index, row
            )
            row = prepare(
                root, stage / root["id"], identifier, plan["protected"], persist
            )
            apply(row, persist)
        if adapter:
            for index, transaction in enumerate(
                manager._job(identifier).get("service_database_journal", [])
            ):
                adapter.apply_restore(
                    transaction,
                    lambda row, index=index: self._persist_database(
                        identifier, index, row
                    ),
                )
        # From this durable point recovery finishes the accepted new state. It must
        # never silently put back an old database after applications can write.
        manager._progress(
            identifier,
            "committing",
            "The selected service state is committed; finishing database cleanup and resuming affected apps.",
            service_restore_committed=True,
            restart_config_sha256=hashlib.sha256(
                Path(manager.config_manager.file_path).read_bytes()
            ).hexdigest(),
            rollback_confirmation_text=f"FINISH SERVICE RESTORE {identifier}",
        )
        manager.config_manager.config = deepcopy(merged)
        self.finish(identifier, adapter=adapter)

    def _ensure_postgres(self, adapter, transactions):
        manager = self.manager
        for transaction in transactions:
            adapter.verify_server_binding(transaction)
        service = manager.config.get("postgres") or {}
        name = service.get("process_name") or "PostgreSQL"
        process = manager.handler.process_names.get(
            manager.handler._prefixed_name(name)
        )
        external = getattr(manager.handler, "external_processes", {}).get(name)
        if (process is not None and process.poll() is None) or (
            external and manager._pid_alive(external)
        ):
            return
        start = getattr(manager.handler, "start_prepared_process", None)
        if start is None:
            raise InstallationArchiveError(
                "Prepared PostgreSQL startup is unavailable; restart recovery requires operator review."
            )
        outcome = start(name)
        if isinstance(outcome, tuple) and outcome[0] is False:
            raise InstallationArchiveError(
                "Managed PostgreSQL could not start for selected database recovery."
            )

    def finish(
        self, identifier, *, adapter=None, recovery=False, require_restart=False
    ):
        manager = self.manager
        job = manager._job(identifier)
        if not job.get("service_restore_committed"):
            raise InstallationArchiveError(
                "The selected restore has not reached its durable commit point."
            )
        expected = job.get("restart_config_sha256")
        if (
            hashlib.sha256(
                Path(manager.config_manager.file_path).read_bytes()
            ).hexdigest()
            != expected
        ):
            raise InstallationArchiveError(
                "The committed selected-service configuration changed; recovery requires review."
            )
        transactions = job.get("service_database_journal", [])
        if transactions:
            adapter = adapter or PostgresServiceBackup(manager.config)
            if recovery:
                self._ensure_postgres(adapter, transactions)
            for index, transaction in enumerate(transactions):
                adapter.finish_restore(
                    transaction,
                    lambda row, index=index: self._persist_database(
                        identifier, index, row
                    ),
                )
        manager.config_manager.config = json.loads(
            Path(manager.config_manager.file_path).read_text()
        )
        if not require_restart:
            manager._resume(identifier)
        manager._finish(
            identifier,
            status="completed",
            stage="restart_required" if require_restart else "completed",
            message=(
                "Selected service recovery finished. Restart the container to resume normal DUMB startup."
                if require_restart
                else "Selected services restored and the previously running affected apps resumed. Unselected state was preserved."
            ),
            can_rollback=False,
            restart_required=require_restart,
            result={
                "complete": True,
                "restart_required": require_restart,
                "retained_previous_paths": [
                    row["holder"]
                    for row in manager._job(identifier).get("rollback_journal", [])
                ],
            },
        )

    def recover(self, identifier, *, same_boot):
        from utils.installation_backup import _BOOT_HOLD

        manager = self.manager
        job = manager._job(identifier)
        restart = bool(not same_boot or manager.boot_error or _BOOT_HOLD)
        if job.get("service_restore_committed"):
            self.finish(identifier, recovery=True, require_restart=restart)
            return
        # Before commit no selected app has been restarted. Any unexpected live
        # writer prevents rollback rather than overwriting its new work.
        if job.get("mutated") and affected_running_rows(
            manager.config,
            job["plan"]["service_scope"]["service_ids"],
            manager._running(),
        ):
            raise InstallationArchiveError(
                "A selected app or dependant is running; pause it before restoring retained originals."
            )
        transactions = job.get("service_database_journal", [])
        if transactions:
            adapter = PostgresServiceBackup(manager.config)
            self._ensure_postgres(adapter, transactions)
            for index in reversed(range(len(transactions))):
                adapter.rollback_restore(
                    transactions[index],
                    lambda row, index=index: self._persist_database(
                        identifier, index, row
                    ),
                )
        if job.get("mutated"):
            manager._rollback_roots(identifier, manager._job(identifier))
        manager.config_manager.config = json.loads(
            Path(manager.config_manager.file_path).read_text()
        )
        # A new controller may have loaded the partially replaced file; only the
        # exact retained original hash and owned file transactions authorize reload.
        if not restart:
            manager._resume(identifier)
        manager._finish(
            identifier,
            status="rolled_back",
            stage="restart_required" if restart else "rolled_back",
            message=(
                "Selected service originals restored. Restart the container to resume normal DUMB startup."
                if restart
                else "Selected service originals restored and their previous running set resumed."
            ),
            can_rollback=False,
            restart_required=restart,
            restart_config_sha256=hashlib.sha256(
                Path(manager.config_manager.file_path).read_bytes()
            ).hexdigest(),
            result={"restart_required": restart},
        )
