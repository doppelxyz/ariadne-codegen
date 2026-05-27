import ast
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Set

from graphql import FragmentDefinitionNode, GraphQLSchema, OperationDefinitionNode

from ..codegen import generate_import_from
from ..exceptions import ParsingError
from ..plugins.manager import PluginManager
from ..settings import ClientSettings, CommentsStrategy
from ..utils import (
    ast_to_raw_str,
    batch_format_files,
    process_name,
    str_to_pascal_case,
)
from .arguments import ArgumentsGenerator
from .client import ClientGenerator
from .comments import get_comment
from .constants import (
    BASE_MODEL_CLASS_NAME,
    BASE_MODEL_FILE_PATH,
    BASE_MODEL_IMPORT,
    DEFAULT_ASYNC_BASE_CLIENT_OPEN_TELEMETRY_PATH,
    DEFAULT_ASYNC_BASE_CLIENT_PATH,
    DEFAULT_BASE_CLIENT_OPEN_TELEMETRY_PATH,
    DEFAULT_BASE_CLIENT_PATH,
    EXCEPTIONS_FILE_PATH,
    GRAPHQL_CLIENT_EXCEPTIONS_NAMES,
    UNSET_IMPORT,
    UPLOAD_CLASS_NAME,
    UPLOAD_IMPORT,
)
from .enums import EnumsGenerator
from .fragments import FragmentsGenerator
from .init_file import InitFileGenerator
from .input_types import InputTypesGenerator
from .result_types import ResultTypesGenerator
from .scalars import ScalarData


class PackageGenerator:
    def __init__(
        self,
        package_name: str,
        target_path: str,
        schema: GraphQLSchema,
        init_generator: InitFileGenerator,
        client_generator: ClientGenerator,
        enums_generator: EnumsGenerator,
        input_types_generator: InputTypesGenerator,
        fragments_generator: FragmentsGenerator,
        fragments_definitions: Optional[Dict[str, FragmentDefinitionNode]] = None,
        client_name: str = "Client",
        async_client: bool = True,
        base_client_name: str = "AsyncBaseClient",
        base_client_file_path: str = DEFAULT_ASYNC_BASE_CLIENT_PATH.as_posix(),
        client_file_name: str = "client",
        enums_module_name: str = "enums",
        input_types_module_name: str = "input_types",
        fragments_module_name: str = "fragments",
        comments_strategy: CommentsStrategy = CommentsStrategy.STABLE,
        queries_source: str = "",
        schema_source: str = "",
        convert_to_snake_case: bool = True,
        include_all_inputs: bool = True,
        include_all_enums: bool = True,
        base_model_file_path: str = BASE_MODEL_FILE_PATH.as_posix(),
        base_model_import: ast.ImportFrom = BASE_MODEL_IMPORT,
        upload_import: ast.ImportFrom = UPLOAD_IMPORT,
        unset_import: ast.ImportFrom = UNSET_IMPORT,
        files_to_include: Optional[List[str]] = None,
        custom_scalars: Optional[Dict[str, ScalarData]] = None,
        plugin_manager: Optional[PluginManager] = None,
    ) -> None:
        self.package_path = Path(target_path) / package_name

        self.schema = schema
        self.fragments_definitions = (
            fragments_definitions if fragments_definitions else {}
        )

        self.init_generator = init_generator
        self.client_generator = client_generator
        self.enums_generator = enums_generator
        self.input_types_generator = input_types_generator
        self.fragments_generator = fragments_generator

        self.client_name = client_name
        self.async_client = async_client
        self.base_client_name = base_client_name
        self.base_client_file_path = Path(base_client_file_path)

        self.client_file_name = client_file_name
        self.enums_module_name = enums_module_name
        self.input_types_module_name = input_types_module_name
        self.fragments_module_name = fragments_module_name

        self.comments_strategy = comments_strategy
        self.queries_source = queries_source
        self.schema_source = schema_source

        self.convert_to_snake_case = convert_to_snake_case
        self.include_all_inputs = include_all_inputs
        self.include_all_enums = include_all_enums

        self.base_model_file_path = Path(base_model_file_path)
        self.base_model_import = base_model_import
        self.upload_import = upload_import
        self.unset_import = unset_import

        self.files_to_include = (
            [Path(f) for f in files_to_include] if files_to_include else []
        )
        self.custom_scalars = custom_scalars if custom_scalars else {}
        self.plugin_manager = plugin_manager

        self._result_types_files: dict[str, ast.Module] = {}
        self._generated_files: list[str] = []
        self._unpacked_fragments: set[str] = set()
        self._used_enums: list[str] = []
        # Tracks files that need batch ruff formatting at end of generate()
        self._format_with_f401: list[Path] = []
        self._format_without_f401: list[Path] = []

    def generate(self) -> List[str]:
        """Generate package with graphql client."""
        # Reset batch-format lists so generate() is idempotent if called again
        self._format_with_f401 = []
        self._format_without_f401 = []

        self._include_exceptions()
        self._validate_unique_file_names()
        if not self.package_path.exists():
            self.package_path.mkdir()
        self._generate_input_types()
        self._generate_result_types()
        self._generate_fragments()
        self._copy_files()
        self._generate_client()
        self._generate_enums()
        self._generate_init()

        # Single batch ruff pass over all generated files instead of per-file calls.
        batch_format_files(self._format_with_f401, self._format_without_f401)

        return sorted(self._generated_files)

    def _write_generated_file(
        self, file_path: Path, code: str, *, remove_unused_imports: bool = True
    ) -> None:
        """Write raw (unformatted) code and register it for batch ruff formatting."""
        file_path.write_text(code)
        if remove_unused_imports:
            self._format_with_f401.append(file_path)
        else:
            self._format_without_f401.append(file_path)

    def add_operation(self, definition: OperationDefinitionNode):
        """Compute and immediately apply a single operation (sequential path)."""
        result = self._compute_operation(definition)
        self._apply_operation(result)

    def _compute_operation(self, definition: OperationDefinitionNode) -> dict:
        """CPU-heavy part: build the result-type AST for one operation.

        This method is stateless with respect to the PackageGenerator — it only
        reads immutable data (schema, fragments_definitions) so it is safe to
        call concurrently from a thread pool.
        """
        name = definition.name
        if not name:
            raise ParsingError("Query without name.")

        return_type_name = str_to_pascal_case(name.value)
        method_name = process_name(
            name.value,
            convert_to_snake_case=True,
            plugin_manager=self.plugin_manager,
            node=definition,
        )
        module_name = method_name
        file_name = f"{module_name}.py"

        query_types_generator = ResultTypesGenerator(
            schema=self.schema,
            operation_definition=definition,
            enums_module_name=self.enums_module_name,
            fragments_module_name=self.fragments_module_name,
            fragments_definitions=self.fragments_definitions,
            base_model_import=self.base_model_import,
            convert_to_snake_case=self.convert_to_snake_case,
            custom_scalars=self.custom_scalars,
            plugin_manager=self.plugin_manager,
        )
        return {
            "file_name": file_name,
            "module": query_types_generator.generate(),
            "operation_str": query_types_generator.get_operation_as_str(),
            "public_names": query_types_generator.get_generated_public_names(),
            "unpacked_fragments": query_types_generator.get_unpacked_fragments(),
            "used_enums": query_types_generator.get_used_enums(),
            "definition": definition,
            "method_name": method_name,
            "return_type_name": return_type_name,
            "module_name": module_name,
        }

    def _apply_operation(self, result: dict) -> None:
        """Apply the computed result to shared generator state (sequential)."""
        self._unpacked_fragments = self._unpacked_fragments.union(
            result["unpacked_fragments"]
        )
        self._used_enums.extend(result["used_enums"])
        self._result_types_files[result["file_name"]] = result["module"]
        self.init_generator.add_import(result["public_names"], result["module_name"], 1)
        self.client_generator.add_method(
            definition=result["definition"],
            name=result["method_name"],
            return_type=result["return_type_name"],
            return_type_module=result["module_name"],
            operation_str=result["operation_str"],
            async_=self.async_client,
        )

    def _include_exceptions(self):
        if self.base_client_file_path in (
            DEFAULT_ASYNC_BASE_CLIENT_PATH,
            DEFAULT_BASE_CLIENT_PATH,
            DEFAULT_ASYNC_BASE_CLIENT_OPEN_TELEMETRY_PATH,
            DEFAULT_BASE_CLIENT_OPEN_TELEMETRY_PATH,
        ):
            self.files_to_include.append(EXCEPTIONS_FILE_PATH)
            self.init_generator.add_import(
                names=GRAPHQL_CLIENT_EXCEPTIONS_NAMES,
                from_=EXCEPTIONS_FILE_PATH.stem,
                level=1,
            )

    def _validate_unique_file_names(self):
        file_names = (
            [
                f"{self.client_file_name}.py",
                self.base_client_file_path.name,
                self.base_model_file_path.name,
                f"{self.enums_module_name}.py",
                f"{self.input_types_module_name}.py",
                f"{self.fragments_module_name}.py",
            ]
            + list(self._result_types_files.keys())
            + [f.name for f in self.files_to_include]
        )

        if len(file_names) != len(set(file_names)):
            seen = set()
            duplicated_files = {n for n in file_names if n in seen or seen.add(n)}
            raise ParsingError(f"Duplicated file names: {',' .join(duplicated_files)}")

    def _generate_client(self):
        client_file_path = self.package_path / f"{self.client_file_name}.py"
        client_module = self.client_generator.generate()
        raw = ast_to_raw_str(client_module, multiline_strings=True)
        code = self._add_comments_to_code(raw, self.queries_source)
        if self.plugin_manager:
            code = self.plugin_manager.generate_client_code(code)
        self._write_generated_file(client_file_path, code, remove_unused_imports=True)
        self._generated_files.append(client_file_path.name)
        self._used_enums.extend(
            self.client_generator.arguments_generator.get_used_enums()
        )
        self.init_generator.add_import(
            names=[self.client_generator.name], from_=self.client_file_name, level=1
        )

    def _add_comments_to_code(self, code: str, source: Optional[str] = None) -> str:
        comment = get_comment(strategy=self.comments_strategy, source=source)
        if self.plugin_manager:
            comment = self.plugin_manager.get_file_comment(
                comment, code=code, source=source
            )
        if comment:
            return comment + "\n\n" + code

        return code

    def _generate_enums(self):
        if self.include_all_enums:
            module = self.enums_generator.generate()
        else:
            module = self.enums_generator.generate(types_to_include=self._used_enums)

        code = self._add_comments_to_code(ast_to_raw_str(module), self.schema_source)
        if self.plugin_manager:
            code = self.plugin_manager.generate_enums_code(code)
        enums_file_path = self.package_path / f"{self.enums_module_name}.py"
        self._write_generated_file(enums_file_path, code, remove_unused_imports=True)
        self._generated_files.append(enums_file_path.name)
        self.init_generator.add_import(
            self.enums_generator.get_generated_public_names(), self.enums_module_name, 1
        )

    def _generate_input_types(self):
        if self.include_all_inputs:
            module = self.input_types_generator.generate()
        else:
            used_inputs = self.client_generator.arguments_generator.get_used_inputs()
            module = self.input_types_generator.generate(types_to_include=used_inputs)

        input_types_file_path = self.package_path / f"{self.input_types_module_name}.py"
        code = self._add_comments_to_code(ast_to_raw_str(module), self.schema_source)
        if self.plugin_manager:
            code = self.plugin_manager.generate_inputs_code(code)
        self._write_generated_file(
            input_types_file_path, code, remove_unused_imports=True
        )
        self._generated_files.append(input_types_file_path.name)
        self._used_enums.extend(self.input_types_generator.get_used_enums())
        self.init_generator.add_import(
            self.input_types_generator.get_generated_public_names(),
            self.input_types_module_name,
            1,
        )

    def _generate_result_types(self):
        def _process_one(item: tuple[str, ast.Module]) -> Path:
            file_name, module = item
            file_path = self.package_path / file_name
            code = self._add_comments_to_code(
                ast_to_raw_str(module), self.queries_source
            )
            if self.plugin_manager:
                code = self.plugin_manager.generate_result_types_code(code)
            file_path.write_text(code, encoding="utf-8")
            return file_path

        with ThreadPoolExecutor() as executor:
            paths = list(executor.map(_process_one, self._result_types_files.items()))

        for file_path in paths:
            self._format_with_f401.append(file_path)
            self._generated_files.append(file_path.name)

    def _generate_fragments(self):
        if not set(self.fragments_definitions.keys()).difference(
            self._unpacked_fragments
        ):
            return

        module = self.fragments_generator.generate(
            exclude_names=self._unpacked_fragments
        )
        file_path = self.package_path / f"{self.fragments_module_name}.py"
        code = self._add_comments_to_code(ast_to_raw_str(module), self.queries_source)
        self._write_generated_file(file_path, code, remove_unused_imports=True)
        self._generated_files.append(file_path.name)
        self._used_enums.extend(self.fragments_generator.get_used_enums())
        self.init_generator.add_import(
            self.fragments_generator.get_generated_public_names(),
            self.fragments_module_name,
            1,
        )

    def _copy_files(self):
        files_to_copy = self.files_to_include + [
            self.base_client_file_path,
            self.base_model_file_path,
        ]
        for source_path in files_to_copy:
            code = self._add_comments_to_code(source_path.read_text(encoding="utf-8"))
            if self.plugin_manager:
                code = self.plugin_manager.copy_code(code)
            target_path = self.package_path / source_path.name
            target_path.write_text(code)
            self._generated_files.append(target_path.name)

        self.init_generator.add_import(
            names=[self.base_client_name],
            from_=self.base_client_file_path.stem,
            level=1,
        )
        self.init_generator.add_import(
            names=[BASE_MODEL_CLASS_NAME, UPLOAD_CLASS_NAME],
            from_=self.base_model_file_path.stem,
            level=1,
        )

    def _generate_init(self):
        init_file_path = self.package_path / "__init__.py"
        init_module = self.init_generator.generate()
        code = self._add_comments_to_code(ast_to_raw_str(init_module))
        if self.plugin_manager:
            code = self.plugin_manager.generate_init_code(code)
        # skip F401: __init__.py uses re-export imports that look "unused" to ruff
        self._write_generated_file(init_file_path, code, remove_unused_imports=False)
        self._generated_files.append(init_file_path.name)

    def _generate_custom_queries(self):
        assert self.custom_query_generator is not None
        file_path = self.package_path / "custom_queries.py"
        module = self.custom_query_generator.generate()
        code = self._add_comments_to_code(ast_to_raw_str(module))
        self._write_generated_file(file_path, code, remove_unused_imports=False)
        self._generated_files.append(file_path.name)

    def _generate_custom_mutations(self):
        assert self.custom_mutation_generator is not None
        file_path = self.package_path / "custom_mutations.py"
        module = self.custom_mutation_generator.generate()
        code = self._add_comments_to_code(ast_to_raw_str(module))
        self._write_generated_file(file_path, code, remove_unused_imports=False)
        self._generated_files.append(file_path.name)

    def _generate_custom_fields_typing(self):
        assert self.custom_fields_typing_generator is not None
        file_path = self.package_path / "custom_typing_fields.py"
        module = self.custom_fields_typing_generator.generate()
        code = self._add_comments_to_code(ast_to_raw_str(module))
        self._write_generated_file(file_path, code, remove_unused_imports=False)
        self._generated_files.append(file_path.name)

    def _generate_custom_fields(self):
        assert self.custom_fields_generator is not None
        file_path = self.package_path / "custom_fields.py"
        module = self.custom_fields_generator.generate()
        code = self._add_comments_to_code(ast_to_raw_str(module))
        self._write_generated_file(file_path, code, remove_unused_imports=False)
        self._generated_files.append(file_path.name)


def get_package_generator(
    schema: GraphQLSchema,
    fragments: List[FragmentDefinitionNode],
    settings: ClientSettings,
    plugin_manager: PluginManager,
) -> PackageGenerator:
    init_generator = InitFileGenerator(plugin_manager=plugin_manager)
    client_generator = ClientGenerator(
        base_client_import=generate_import_from(
            names=[settings.base_client_name],
            from_=Path(settings.base_client_file_path).stem,
            level=1,
        ),
        arguments_generator=ArgumentsGenerator(
            schema=schema,
            convert_to_snake_case=settings.convert_to_snake_case,
            custom_scalars=settings.scalars,
            plugin_manager=plugin_manager,
        ),
        name=settings.client_name,
        base_client=settings.base_client_name,
        enums_module_name=settings.enums_module_name,
        input_types_module_name=settings.input_types_module_name,
        unset_import=UNSET_IMPORT,
        upload_import=UPLOAD_IMPORT,
        custom_scalars=settings.scalars,
        plugin_manager=plugin_manager,
    )
    enums_generator = EnumsGenerator(schema=schema, plugin_manager=plugin_manager)
    input_types_generator = InputTypesGenerator(
        schema=schema,
        enums_module=settings.enums_module_name,
        base_model_import=BASE_MODEL_IMPORT,
        upload_import=UPLOAD_IMPORT,
        convert_to_snake_case=settings.convert_to_snake_case,
        custom_scalars=settings.scalars,
        plugin_manager=plugin_manager,
    )
    fragments_definitions = {f.name.value: f for f in fragments or []}
    fragments_generator = FragmentsGenerator(
        schema=schema,
        fragments_definitions=fragments_definitions,
        enums_module_name=settings.enums_module_name,
        base_model_import=BASE_MODEL_IMPORT,
        convert_to_snake_case=settings.convert_to_snake_case,
        custom_scalars=settings.scalars,
        plugin_manager=plugin_manager,
    )

    return PackageGenerator(
        package_name=settings.target_package_name,
        target_path=settings.target_package_path,
        schema=schema,
        init_generator=init_generator,
        client_generator=client_generator,
        enums_generator=enums_generator,
        input_types_generator=input_types_generator,
        fragments_generator=fragments_generator,
        fragments_definitions=fragments_definitions,
        client_name=settings.client_name,
        async_client=settings.async_client,
        base_client_name=settings.base_client_name,
        base_client_file_path=settings.base_client_file_path,
        client_file_name=settings.client_file_name,
        enums_module_name=settings.enums_module_name,
        input_types_module_name=settings.input_types_module_name,
        fragments_module_name=settings.fragments_module_name,
        comments_strategy=settings.include_comments,
        queries_source=settings.queries_path,
        schema_source=settings.schema_source,
        convert_to_snake_case=settings.convert_to_snake_case,
        include_all_inputs=settings.include_all_inputs,
        include_all_enums=settings.include_all_enums,
        base_model_file_path=BASE_MODEL_FILE_PATH.as_posix(),
        base_model_import=BASE_MODEL_IMPORT,
        upload_import=UPLOAD_IMPORT,
        unset_import=UNSET_IMPORT,
        files_to_include=settings.files_to_include,
        custom_scalars=settings.scalars,
        plugin_manager=plugin_manager,
    )


# Module-level reference used by _compute_op_worker so that fork-based
# ProcessPoolExecutor workers inherit it without pickling the heavy schema.
_fork_package_gen: Optional["PackageGenerator"] = None


def _compute_op_worker(definition: OperationDefinitionNode) -> dict:
    """Worker executed in a forked child process.

    _fork_package_gen is inherited from the parent via fork (copy-on-write),
    so the GraphQLSchema and all generator state are free to use here.
    Only the OperationDefinitionNode arg and the returned dict cross the
    process boundary (via pickle over a pipe).
    """
    assert _fork_package_gen is not None
    return _fork_package_gen._compute_operation(definition)


def parallel_compute_operations(
    package_gen: "PackageGenerator",
    queries: list[OperationDefinitionNode],
) -> list[dict]:
    """Run _compute_operation in parallel using forked processes.

    Falls back to sequential execution when:
    - queries is empty (avoids max_workers=0 ValueError)
    - plugins are active (plugin hooks mutate state in child processes that
      the parent needs; fork does not propagate those mutations back)
    - fork is unavailable (e.g. Windows)
    """
    has_plugins = bool(
        package_gen.plugin_manager and package_gen.plugin_manager.plugins
    )
    if not queries or os.name == "nt" or has_plugins:
        return [package_gen._compute_operation(q) for q in queries]

    global _fork_package_gen
    _fork_package_gen = package_gen
    try:
        ctx = multiprocessing.get_context("fork")
        workers = min(len(queries), os.cpu_count() or 4)
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
            return list(pool.map(_compute_op_worker, queries))
    finally:
        _fork_package_gen = None
