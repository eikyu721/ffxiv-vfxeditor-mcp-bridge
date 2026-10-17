"""Small Havok 2014 binary tagfile reader and conservative writer.

The format logic is independently implemented from the public MIT-licensed
exyorha/hkxparse tagfile reader.  It never loads Havok native code and never
The writer only serializes object graphs that the reader consumed completely;
callers are responsible for rejecting unsupported SKLB container variants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import struct
from typing import Any


class HavokTagfileError(ValueError):
    pass


@dataclass
class Member:
    name: str
    kind: int
    tuple_size: int = 0
    class_name: str = ""


@dataclass
class TypeInfo:
    name: str
    parent: int
    members: list[Member] = field(default_factory=list)
    unknown: int = 0


class Reader:
    BASIC_MASK = 15
    ARRAY = 16
    TUPLE = 32
    BYTE, INT, REAL, VEC4, VEC8, VEC12, VEC16, OBJECT, STRUCT, CSTRING = range(1, 11)

    def __init__(self, data: bytes):
        if len(data) < 8:
            raise HavokTagfileError("Havok payload is shorter than its tagfile header.")
        little = struct.unpack_from("<II", data)[0:2] == (0xCAB00D1E, 0xD011FACE)
        big = struct.unpack_from(">II", data)[0:2] == (0xCAB00D1E, 0xD011FACE)
        if not little and not big:
            raise HavokTagfileError("Havok tagfile magic is invalid or unsupported.")
        self.data = data
        self.pos = 8
        self.endian = "<" if little else ">"
        self.strings: list[str] = ["", ""]
        self.types: list[TypeInfo] = [TypeInfo("BuiltinVoidType", 0, [])]
        self.type_lookup: dict[str, int] = {}
        self.objects: dict[int, dict[str, Any]] = {}
        self.next_object = 1
        self.havok_version: str | None = None
        self.tagfile_version: int | None = None
        self.file_layout: list[int] | None = None
        self.array_prefixes: dict[int, int] = {}
        self.struct_array_prefixes: dict[tuple[int, str], int] = {}

    def read(self, size: int) -> bytes:
        if size < 0 or self.pos + size > len(self.data):
            raise HavokTagfileError(f"Out-of-bounds tagfile read at {self.pos} (+{size}).")
        out = self.data[self.pos:self.pos + size]
        self.pos += size
        return out

    def u8(self) -> int:
        return self.read(1)[0]

    def f32(self) -> float:
        return struct.unpack(self.endian + "f", self.read(4))[0]

    def varint(self) -> int:
        byte = self.u8()
        negative = bool(byte & 1)
        value = (byte & 0x7E) >> 1
        shift = 6
        while byte & 0x80:
            byte = self.u8()
            value |= (byte & 0x7F) << shift
            shift += 7
            if shift > 69:
                raise HavokTagfileError("Tagfile variable integer is too long.")
        return -value if negative else value

    def string(self) -> str:
        length = self.varint()
        if length <= 0:
            index = -length
            if index >= len(self.strings):
                raise HavokTagfileError(f"Invalid tagfile string-pool reference {index}.")
            return self.strings[index]
        try:
            value = self.read(length).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HavokTagfileError("Tagfile contains a non-UTF-8 metadata string.") from exc
        self.strings.append(value)
        return value

    def type_info(self) -> TypeInfo:
        name = self.string()
        unknown = self.varint()
        parent = self.varint()
        count = self.varint()
        if parent < 0 or parent >= len(self.types) or count < 0 or count > 4096:
            raise HavokTagfileError("Invalid tagfile type metadata.")
        members: list[Member] = []
        for _ in range(count):
            member_name = self.string()
            kind = self.varint()
            tuple_size = self.varint() if kind & self.TUPLE else 0
            basic = kind & self.BASIC_MASK
            class_name = self.string() if basic in (self.OBJECT, self.STRUCT) else ""
            members.append(Member(member_name, kind, tuple_size, class_name))
        return TypeInfo(name, parent, members, unknown)

    def member_count(self, class_index: int) -> int:
        count = 0
        visited: set[int] = set()
        while class_index:
            if class_index in visited or class_index >= len(self.types):
                raise HavokTagfileError("Invalid or cyclic tagfile type inheritance.")
            visited.add(class_index)
            info = self.types[class_index]
            count += len(info.members)
            class_index = info.parent
        return count

    def members_in_order(self, class_index: int) -> list[Member]:
        lineage: list[TypeInfo] = []
        while class_index:
            info = self.types[class_index]
            lineage.append(info)
            class_index = info.parent
        return [member for info in reversed(lineage) for member in info.members]

    def parse_struct(self, target: dict[str, Any], class_index: int = 0) -> None:
        if class_index == 0:
            class_index = self.varint()
        if class_index <= 0 or class_index >= len(self.types):
            raise HavokTagfileError(f"Invalid object type index {class_index}.")
        members = self.members_in_order(class_index)
        bitmap = self.read((len(members) + 7) // 8)
        lineage: list[str] = []
        idx = class_index
        while idx:
            lineage.append(self.types[idx].name)
            idx = self.types[idx].parent
        target["$classes"] = list(reversed(lineage))
        fields = target.setdefault("$fields", {})
        for index, member in enumerate(members):
            if bitmap[index // 8] & (1 << (index % 8)):
                fields[member.name] = self.parse_field(member)

    def parse_field(self, member: Member) -> Any:
        allowed = self.ARRAY | self.TUPLE | self.BASIC_MASK
        if member.kind & ~allowed:
            raise HavokTagfileError(f"Unsupported tagfile member flags 0x{member.kind:X}.")
        if member.kind == self.TUPLE | self.BYTE:
            return list(self.read(member.tuple_size))
        if member.kind == self.ARRAY | self.BYTE:
            count = self.varint()
            return list(self.read(count))
        if member.kind & (self.ARRAY | self.TUPLE):
            if member.kind & self.ARRAY and member.kind & self.TUPLE:
                raise HavokTagfileError("Array/tuple combination is unsupported.")
            count = member.tuple_size if member.kind & self.TUPLE else self.varint()
            if count < 0 or count > 1_000_000:
                raise HavokTagfileError(f"Invalid array size {count}.")
            return self.parse_array(member, count)
        return self.parse_value(member.kind & self.BASIC_MASK, member.class_name, -1)

    def array_prefix(self, kind: int) -> int:
        basic = kind & self.BASIC_MASK
        return self.varint() if basic in (self.INT, self.VEC4) else -1

    def parse_array(self, member: Member, count: int) -> list[Any]:
        prefix = self.array_prefix(member.kind)
        basic = member.kind & self.BASIC_MASK
        if basic == self.STRUCT:
            value = self.parse_struct_array(member, count)
        else:
            value = [self.parse_value(basic, member.class_name, prefix) for _ in range(count)]
        if prefix >= 0:
            self.array_prefixes[id(value)] = prefix
        return value

    def parse_value(self, kind: int, class_name: str, array_prefix: int) -> Any:
        if kind == self.BYTE:
            return self.u8()
        if kind == self.INT:
            return self.varint()
        if kind == self.REAL:
            return self.f32()
        if kind == self.VEC4:
            count = 4 if array_prefix < 0 else array_prefix
            if count < 1 or count > 4:
                raise HavokTagfileError(f"Unsupported vector width {count}.")
            return [self.f32() for _ in range(count)] + [0.0] * (4 - count)
        if kind == self.VEC12:
            return [[self.f32() for _ in range(4)] for _ in range(3)]
        if kind == self.VEC16:
            return [[self.f32() for _ in range(4)] for _ in range(4)]
        if kind == self.OBJECT:
            object_index = self.varint()
            if object_index == 0:
                return None
            return self.objects.setdefault(object_index, {})
        if kind == self.STRUCT:
            if class_name not in self.type_lookup:
                raise HavokTagfileError(f"Unknown inline struct type {class_name!r}.")
            value: dict[str, Any] = {}
            self.parse_struct(value, self.type_lookup[class_name])
            return value
        if kind == self.CSTRING:
            return self.string()
        raise HavokTagfileError(f"Unsupported tagfile basic type {kind}.")

    def parse_struct_array(self, member: Member, count: int) -> list[Any]:
        if member.class_name:
            class_index = self.type_lookup.get(member.class_name, 0)
        else:
            class_index = self.varint()
        if not class_index:
            raise HavokTagfileError("Struct array has no resolvable class type.")
        members = self.members_in_order(class_index)
        bitmap = self.read((len(members) + 7) // 8)
        lineage: list[str] = []
        idx = class_index
        while idx:
            lineage.append(self.types[idx].name)
            idx = self.types[idx].parent
        values = [{"$classes": list(reversed(lineage)), "$fields": {}} for _ in range(count)]
        for index, child_member in enumerate(members):
            if bitmap[index // 8] & (1 << (index % 8)):
                column = self.parse_array(child_member, count)
                prefix = self.array_prefixes.pop(id(column), None)
                if prefix is not None:
                    self.struct_array_prefixes[(id(values), child_member.name)] = prefix
                for row, item in zip(values, column, strict=True):
                    row["$fields"][child_member.name] = item
        return values

    def parse(self) -> dict[str, Any]:
        while True:
            tag = self.varint()
            if tag == 1:
                version = self.varint()
                if version not in (3, 4, 5):
                    raise HavokTagfileError(f"Unsupported Havok tagfile version {version}.")
                self.tagfile_version = version
                self.strings = ["", ""]
                if version in (4, 5):
                    self.havok_version = self.string()
                if version == 5:
                    # Havok 2014.2 FFXIV tagfiles carry six file-layout bytes after the
                    # version string. They are metadata, not a top-level tag.
                    self.file_layout = list(self.read(6))
            elif tag == 2:
                info = self.type_info()
                self.type_lookup[info.name] = len(self.types)
                self.types.append(info)
            elif tag == 4:
                index = self.next_object
                target = self.objects.setdefault(index, {})
                self.next_object += 1
                self.parse_struct(target)
            elif tag == 7:
                break
            else:
                raise HavokTagfileError(f"Unsupported top-level tag {tag} at offset {self.pos}.")
        unresolved = [index for index, value in self.objects.items() if "$classes" not in value]
        if unresolved:
            raise HavokTagfileError(f"Unresolved tagfile object references: {unresolved}.")
        if 1 not in self.objects:
            raise HavokTagfileError("Tagfile has no root object.")
        return self.objects[1]


def parse_tagfile(data: bytes) -> tuple[dict[str, Any], str | None]:
    reader = Reader(data)
    return reader.parse(), reader.havok_version


class Writer:
    """Serialize a graph produced by :class:`Reader` without native Havok."""

    BASIC_MASK = Reader.BASIC_MASK
    ARRAY = Reader.ARRAY
    TUPLE = Reader.TUPLE
    BYTE, INT, REAL, VEC4, VEC8, VEC12, VEC16, OBJECT, STRUCT, CSTRING = range(1, 11)

    def __init__(self, reader: Reader):
        if reader.tagfile_version not in (3, 4, 5):
            raise HavokTagfileError("Reader has no supported completed tagfile version.")
        if reader.pos != len(reader.data):
            raise HavokTagfileError("Refusing to serialize a tagfile that was not fully consumed.")
        self.source = reader
        self.endian = reader.endian
        self.data = bytearray()
        self.object_indices = {id(value): index for index, value in reader.objects.items()}

    def write(self, payload: bytes) -> None:
        self.data.extend(payload)

    def u8(self, value: int) -> None:
        if type(value) is not int or not 0 <= value <= 255:
            raise HavokTagfileError(f"Byte value is out of range: {value!r}.")
        self.data.append(value)

    def f32(self, value: Any) -> None:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise HavokTagfileError(f"Value is not a float: {value!r}.") from exc
        self.write(struct.pack(self.endian + "f", number))

    def varint(self, value: int) -> None:
        if type(value) is not int:
            raise HavokTagfileError(f"Variable integer is not an int: {value!r}.")
        negative = value < 0
        remaining = -value if negative else value
        first = ((remaining & 0x3F) << 1) | int(negative)
        remaining >>= 6
        if remaining:
            first |= 0x80
        self.u8(first)
        while remaining:
            byte = remaining & 0x7F
            remaining >>= 7
            if remaining:
                byte |= 0x80
            self.u8(byte)

    def string(self, value: str) -> None:
        if not isinstance(value, str):
            raise HavokTagfileError(f"Tagfile string is not text: {value!r}.")
        if not value:
            self.varint(0)
            return
        encoded = value.encode("utf-8")
        self.varint(len(encoded))
        self.write(encoded)

    def type_info(self, info: TypeInfo) -> None:
        self.string(info.name)
        self.varint(info.unknown)
        self.varint(info.parent)
        self.varint(len(info.members))
        for member in info.members:
            self.string(member.name)
            self.varint(member.kind)
            if member.kind & self.TUPLE:
                self.varint(member.tuple_size)
            if member.kind & self.BASIC_MASK in (self.OBJECT, self.STRUCT):
                self.string(member.class_name)

    def _class_index(self, value: dict[str, Any]) -> int:
        classes = value.get("$classes")
        if not isinstance(classes, list) or not classes:
            raise HavokTagfileError("Structured value has no class lineage.")
        index = self.source.type_lookup.get(classes[-1], 0)
        if not index:
            raise HavokTagfileError(f"Unknown structured value class {classes[-1]!r}.")
        return index

    def write_struct(self, value: dict[str, Any], class_index: int = 0) -> None:
        if not class_index:
            class_index = self._class_index(value)
            self.varint(class_index)
        members = self.source.members_in_order(class_index)
        fields = value.get("$fields", {})
        if not isinstance(fields, dict):
            raise HavokTagfileError("Structured value fields are invalid.")
        known = {member.name for member in members}
        unexpected = set(fields) - known
        if unexpected:
            raise HavokTagfileError(f"Structured value has unknown fields: {sorted(unexpected)}.")
        bitmap = bytearray((len(members) + 7) // 8)
        for index, member in enumerate(members):
            if member.name in fields:
                bitmap[index // 8] |= 1 << (index % 8)
        self.write(bytes(bitmap))
        for member in members:
            if member.name in fields:
                self.write_field(member, fields[member.name])

    def write_field(self, member: Member, value: Any) -> None:
        allowed = self.ARRAY | self.TUPLE | self.BASIC_MASK
        if member.kind & ~allowed:
            raise HavokTagfileError(f"Unsupported tagfile member flags 0x{member.kind:X}.")
        if member.kind == self.TUPLE | self.BYTE:
            if not isinstance(value, list) or len(value) != member.tuple_size:
                raise HavokTagfileError(f"Byte tuple {member.name!r} has the wrong length.")
            self.write(bytes(value))
            return
        if member.kind == self.ARRAY | self.BYTE:
            if not isinstance(value, list):
                raise HavokTagfileError(f"Byte array {member.name!r} is invalid.")
            self.varint(len(value))
            self.write(bytes(value))
            return
        if member.kind & (self.ARRAY | self.TUPLE):
            if member.kind & self.ARRAY and member.kind & self.TUPLE:
                raise HavokTagfileError("Array/tuple combination is unsupported.")
            if not isinstance(value, list):
                raise HavokTagfileError(f"Array field {member.name!r} is invalid.")
            expected = member.tuple_size if member.kind & self.TUPLE else len(value)
            if member.kind & self.TUPLE and len(value) != expected:
                raise HavokTagfileError(f"Tuple field {member.name!r} has the wrong length.")
            if member.kind & self.ARRAY:
                self.varint(len(value))
            basic = member.kind & self.BASIC_MASK
            if basic in (self.INT, self.VEC4):
                prefix = self.source.array_prefixes.get(id(value))
                if prefix is None:
                    raise HavokTagfileError(f"Array prefix for field {member.name!r} was not preserved.")
                self.varint(prefix)
            self.write_array(member, value, prefix if basic in (self.INT, self.VEC4) else None)
            return
        self.write_value(member.kind & self.BASIC_MASK, member.class_name, value, -1)

    def write_array(self, member: Member, values: list[Any], explicit_prefix: int | None = None) -> None:
        basic = member.kind & self.BASIC_MASK
        if basic == self.STRUCT:
            self.write_struct_array(member, values)
            return
        prefix = explicit_prefix if explicit_prefix is not None else self.source.array_prefixes.get(id(values), -1)
        for value in values:
            self.write_value(basic, member.class_name, value, prefix)

    def write_value(self, kind: int, class_name: str, value: Any, array_prefix: int) -> None:
        if kind == self.BYTE:
            self.u8(value)
        elif kind == self.INT:
            self.varint(value)
        elif kind == self.REAL:
            self.f32(value)
        elif kind == self.VEC4:
            count = 4 if array_prefix < 0 else array_prefix
            if not isinstance(value, list) or len(value) != 4 or not 1 <= count <= 4:
                raise HavokTagfileError("VEC4 value or preserved width is invalid.")
            for number in value[:count]:
                self.f32(number)
        elif kind in (self.VEC12, self.VEC16):
            rows = 3 if kind == self.VEC12 else 4
            if not isinstance(value, list) or len(value) != rows or any(not isinstance(row, list) or len(row) != 4 for row in value):
                raise HavokTagfileError(f"VEC{rows * 4} value is invalid.")
            for row in value:
                for number in row:
                    self.f32(number)
        elif kind == self.OBJECT:
            if value is None:
                self.varint(0)
            else:
                index = self.object_indices.get(id(value))
                if index is None:
                    raise HavokTagfileError(f"Object reference for {class_name!r} is not in the object table.")
                self.varint(index)
        elif kind == self.STRUCT:
            class_index = self.source.type_lookup.get(class_name, 0)
            if not class_index:
                raise HavokTagfileError(f"Unknown inline struct type {class_name!r}.")
            self.write_struct(value, class_index)
        elif kind == self.CSTRING:
            self.string(value)
        else:
            raise HavokTagfileError(f"Unsupported tagfile basic type {kind}.")

    def write_struct_array(self, member: Member, values: list[dict[str, Any]]) -> None:
        if member.class_name:
            class_index = self.source.type_lookup.get(member.class_name, 0)
        elif values:
            class_index = self._class_index(values[0])
            self.varint(class_index)
        else:
            raise HavokTagfileError("Cannot recover the class of an empty anonymous struct array.")
        if not class_index:
            raise HavokTagfileError(f"Unknown struct-array type {member.class_name!r}.")
        members = self.source.members_in_order(class_index)
        field_sets = [set(value.get("$fields", {})) for value in values]
        present = field_sets[0] if field_sets else set()
        if any(fields != present for fields in field_sets):
            raise HavokTagfileError("Struct-array rows do not share one field bitmap.")
        known = {child.name for child in members}
        if present - known:
            raise HavokTagfileError(f"Struct array has unknown fields: {sorted(present - known)}.")
        bitmap = bytearray((len(members) + 7) // 8)
        for index, child in enumerate(members):
            if child.name in present:
                bitmap[index // 8] |= 1 << (index % 8)
        self.write(bytes(bitmap))
        for child in members:
            if child.name in present:
                column = [value["$fields"][child.name] for value in values]
                basic = child.kind & self.BASIC_MASK
                prefix = None
                if basic in (self.INT, self.VEC4):
                    prefix = self.source.struct_array_prefixes.get((id(values), child.name))
                    if prefix is None:
                        raise HavokTagfileError(
                            f"Struct-array prefix for field {child.name!r} was not preserved.")
                    self.varint(prefix)
                self.write_array(child, column, prefix)

    def serialize(self) -> bytes:
        self.write(struct.pack(self.endian + "II", 0xCAB00D1E, 0xD011FACE))
        self.varint(1)
        self.varint(self.source.tagfile_version)
        if self.source.tagfile_version in (4, 5):
            self.string(self.source.havok_version or "")
        if self.source.tagfile_version == 5:
            if not isinstance(self.source.file_layout, list) or len(self.source.file_layout) != 6:
                raise HavokTagfileError("Havok v5 file-layout bytes were not preserved.")
            self.write(bytes(self.source.file_layout))
        for info in self.source.types[1:]:
            self.varint(2)
            self.type_info(info)
        for index in range(1, len(self.source.objects) + 1):
            if index not in self.source.objects:
                raise HavokTagfileError("Object table is not contiguous.")
            self.varint(4)
            self.write_struct(self.source.objects[index])
        self.varint(7)
        return bytes(self.data)


def serialize_tagfile(reader: Reader) -> bytes:
    return Writer(reader).serialize()
