# SPDX-License-Identifier: LGPL-2.1-or-later

import os
import re
import zipfile

import FreeCAD
import FreeCADGui
import Mesh
import MeshPart
import Part

from PySide import QtWidgets

from freecad.nestingworkbench.ui_helpers import QT_TRANSLATE_NOOP


def sanitize_filename(name):
    text = str(name or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", "_", text)
    text = text.strip(" ._")
    return text or "plato"


def is_layout_group(obj):
    try:
        return obj.isDerivedFrom("App::DocumentObjectGroup") and obj.Label.lower().startswith("layout")
    except Exception:
        return False


def find_layouts():
    doc = FreeCAD.ActiveDocument
    if not doc:
        return []
    selection = FreeCADGui.Selection.getSelection()
    layouts = []
    for obj in selection:
        if is_layout_group(obj):
            layouts.append(obj)
            continue
        seen = set()
        current = obj
        while current is not None and current.Name not in seen:
            seen.add(current.Name)
            if is_layout_group(current):
                layouts.append(current)
                break
            parents = getattr(current, "InList", []) or []
            current = parents[0] if parents else None
    if not layouts:
        layouts = [obj for obj in doc.Objects if is_layout_group(obj)]
    unique = []
    for layout in layouts:
        if layout not in unique:
            unique.append(layout)
    return unique


def layout_sheets(layout):
    sheets = []
    for child in getattr(layout, "Group", []) or []:
        try:
            is_group = child.isDerivedFrom("App::DocumentObjectGroup")
        except Exception:
            is_group = False
        if is_group and (child.Label.startswith("Sheet_") or child.Name.startswith("Sheet_")):
            sheets.append(child)
    return sheets


def walk_solids(container):
    for child in getattr(container, "Group", []) or []:
        label = getattr(child, "Label", "")
        name = getattr(child, "Name", "")
        try:
            is_group = child.isDerivedFrom("App::DocumentObjectGroup")
        except Exception:
            is_group = False
        if is_group:
            yield from walk_solids(child)
            continue
        if label.startswith("nested_") or name.startswith("nested_"):
            yield from walk_solids(child)
            continue
        if label.startswith("Sheet_Boundary") or name.startswith("Sheet_Boundary"):
            continue
        if label.startswith("bound_") or name.startswith("bound_"):
            continue
        try:
            volume = child.Shape.Volume
        except Exception:
            volume = 0.0
        if volume > 0.001:
            yield child
            continue
        if hasattr(child, "Group"):
            yield from walk_solids(child)


def find_nested_parent(obj):
    current = obj
    seen = set()
    while current is not None and current.Name not in seen:
        seen.add(current.Name)
        parents = getattr(current, "InList", []) or []
        for candidate in parents:
            label = getattr(candidate, "Label", "")
            name = getattr(candidate, "Name", "")
            try:
                is_part = candidate.isDerivedFrom("App::Part")
            except Exception:
                is_part = False
            if is_part and (label.startswith("nested_") or name.startswith("nested_")):
                return candidate
        try:
            current = current.getParentGroup()
        except Exception:
            current = parents[0] if parents else None
    return None


def get_global_shape(obj):
    shape = obj.Shape.copy()
    parent = find_nested_parent(obj)
    if parent is not None:
        try:
            placement = parent.getGlobalPlacement()
        except Exception:
            placement = getattr(parent, "Placement", None)
        if placement is not None and not placement.isIdentity():
            shape.transformShape(placement.toMatrix())
        return shape
    try:
        if obj.isDerivedFrom("App::Part"):
            return shape
    except Exception:
        pass
    try:
        if shape.Placement.isIdentity():
            placement = getattr(obj, "Placement", None)
            if placement is not None and not placement.isIdentity():
                shape.transformShape(placement.toMatrix())
    except Exception:
        pass
    return shape


def build_plate_shape(shapes):
    adjusted = []
    for shape in shapes:
        unit = shape
        try:
            box = unit.BoundBox
            if box.isValid():
                translated = unit.translate(FreeCAD.Vector(0, 0, -box.ZMin))
                if translated is not None:
                    unit = translated
        except Exception:
            pass
        adjusted.append(unit)
    if not adjusted:
        return None
    plate = adjusted[0] if len(adjusted) == 1 else Part.makeCompound(adjusted)
    try:
        box = plate.BoundBox
        if box.isValid():
            translated = plate.translate(FreeCAD.Vector(-box.XMin, -box.YMin, 0))
            if translated is not None:
                plate = translated
    except Exception:
        pass
    return plate


def mesh_from_shape(shape, tolerance):
    if MeshPart is not None:
        try:
            return MeshPart.meshFromShape(
                Shape=shape,
                LinearDeflection=float(tolerance),
                AngularDeflection=0.5,
                Relative=False,
            )
        except Exception:
            pass
    vertices, facets = shape.tessellate(tolerance)
    mesh = Mesh.Mesh()
    mesh.addFacets([[vertices[f[0]], vertices[f[1]], vertices[f[2]]] for f in facets])
    return mesh


def count_sheet_solids(sheet):
    try:
        return len(list(walk_solids(sheet)))
    except Exception:
        return 0


def project_name():
    doc = FreeCAD.ActiveDocument
    if doc and doc.FileName:
        return sanitize_filename(os.path.splitext(os.path.basename(doc.FileName))[0])
    if doc and doc.Name and doc.Name != "Unnamed":
        return sanitize_filename(doc.Name)
    return "proyecto"


def export_layouts(layouts, directory, formats=("3mf",), tolerance=0.2, create_zip=True, base_name=None, progress_callback=None):
    exported = []
    errors = []
    try:
        os.makedirs(directory, exist_ok=True)
    except Exception as exc:
        return [], [f"No se pudo crear la carpeta {directory}: {exc}"], None

    clean_base = sanitize_filename(base_name) if base_name and str(base_name).strip() else project_name()

    tasks = []
    for layout in layouts:
        sheets = layout_sheets(layout)
        if not sheets:
            errors.append(f"{layout.Label}: no se encontraron hojas Sheet_*")
            continue
        for index, sheet in enumerate(sheets, 1):
            solids = list(walk_solids(sheet))
            if not solids:
                errors.append(f"{layout.Label}/{sheet.Label}: no hay sólidos 3D")
                continue
            tasks.append((layout, sheet, index, solids))

    total = len(tasks)
    done = 0
    for layout, sheet, index, solids in tasks:
        try:
            shapes = []
            total_solids = len(solids)
            for position, obj in enumerate(solids, 1):
                shapes.append(get_global_shape(obj))
                if progress_callback and progress_callback(position, total_solids, f"{sheet.Label}: pieza {position}/{total_solids}") is False:
                    errors.append("Cancelado por el usuario")
                    return exported, errors, None
            plate = build_plate_shape(shapes)
            if plate is None:
                raise ValueError("no se pudo construir el plato")
            if progress_callback:
                progress_callback(total_solids, total_solids, f"{sheet.Label}: generando malla...")
            if len(layouts) > 1:
                base = f"{clean_base}_{sanitize_filename(layout.Label)}_plato_{index:02d}"
            else:
                base = f"{clean_base}_plato_{index:02d}"
            mesh = None
            for ext in formats:
                path = os.path.join(directory, f"{base}.{ext}")
                try:
                    if mesh is None:
                        mesh = mesh_from_shape(plate, tolerance)
                    mesh.write(path)
                    exported.append(path)
                    FreeCAD.Console.PrintMessage(f"Exported: {path}\n")
                except Exception as exc:
                    message = f"{layout.Label}/{sheet.Label}/{base}.{ext}: {exc}"
                    errors.append(message)
                    FreeCAD.Console.PrintError(f"Export failed: {message}\n")
        except Exception as exc:
            message = f"{layout.Label}/{sheet.Label}: {exc}"
            errors.append(message)
            FreeCAD.Console.PrintError(f"Export failed: {message}\n")
        done += 1
        if progress_callback and progress_callback(done, total, sheet.Label) is False:
            errors.append("Cancelado por el usuario")
            return exported, errors, None

    zip_path = None
    if create_zip and exported:
        try:
            zip_path = os.path.join(directory, f"{clean_base}_platos.zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in exported:
                    archive.write(path, arcname=os.path.basename(path))
            FreeCAD.Console.PrintMessage(f"ZIP: {zip_path}\n")
        except Exception as exc:
            errors.append(f"ZIP: {exc}")
            zip_path = None

    return exported, errors, zip_path


class ExportPlatesDialog(QtWidgets.QDialog):
    def __init__(self, layouts, parent=None):
        super().__init__(parent)
        self.layouts = layouts
        self.setWindowTitle("Exportar platos 3MF")
        self.setModal(True)
        self.setMinimumWidth(560)
        self._build_ui()
        self._populate_defaults()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        info = QtWidgets.QLabel(
            "Exporta un 3MF por cada Sheet_ (plato), con las piezas ya acomodadas.\n"
            "Los archivos se llamarán <nombre>_plato_01.3mf, <nombre>_plato_02.3mf, etc.\n"
            "No uses Create CAM Job: eso es para CNC, no para impresora 3D."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        listing_group = QtWidgets.QGroupBox("Platos encontrados")
        listing_layout = QtWidgets.QVBoxLayout(listing_group)
        self.listing = QtWidgets.QListWidget()
        self.listing.setMaximumHeight(160)
        self.listing.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        for layout_obj in self.layouts:
            for sheet in layout_sheets(layout_obj):
                self.listing.addItem(
                    f"{layout_obj.Label} / {sheet.Label}: {count_sheet_solids(sheet)} sólido(s)"
                )
        listing_layout.addWidget(self.listing)
        layout.addWidget(listing_group)

        output_group = QtWidgets.QGroupBox("Salida")
        output_layout = QtWidgets.QFormLayout(output_group)

        directory_layout = QtWidgets.QHBoxLayout()
        self.directory_edit = QtWidgets.QLineEdit()
        self.directory_edit.setReadOnly(True)
        browse_button = QtWidgets.QPushButton("Buscar...")
        browse_button.clicked.connect(self._browse_directory)
        directory_layout.addWidget(self.directory_edit)
        directory_layout.addWidget(browse_button)
        output_layout.addRow("Carpeta:", directory_layout)

        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.setPlaceholderText("nombre del proyecto")
        output_layout.addRow("Nombre base:", self.name_edit)

        self.tolerance_spin = QtWidgets.QDoubleSpinBox()
        self.tolerance_spin.setRange(0.001, 10.0)
        self.tolerance_spin.setValue(0.2)
        self.tolerance_spin.setDecimals(3)
        self.tolerance_spin.setSuffix(" mm")
        output_layout.addRow("Tolerancia malla:", self.tolerance_spin)

        self.threemf_checkbox = QtWidgets.QCheckBox("3MF (.3mf) — recomendado para imprimir")
        self.threemf_checkbox.setChecked(True)
        output_layout.addRow("", self.threemf_checkbox)

        self.stl_checkbox = QtWidgets.QCheckBox("También STL (.stl)")
        self.stl_checkbox.setChecked(False)
        output_layout.addRow("", self.stl_checkbox)

        self.zip_checkbox = QtWidgets.QCheckBox("Crear ZIP al terminar")
        self.zip_checkbox.setChecked(True)
        output_layout.addRow("", self.zip_checkbox)

        layout.addWidget(output_group)

        button_box = QtWidgets.QDialogButtonBox()
        button_box.addButton("Exportar", QtWidgets.QDialogButtonBox.AcceptRole)
        button_box.addButton(QtWidgets.QDialogButtonBox.Cancel)
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _populate_defaults(self):
        doc = FreeCAD.ActiveDocument
        if doc and doc.FileName:
            default_directory = os.path.dirname(doc.FileName)
        else:
            default_directory = os.path.join(os.path.expanduser("~"), "Downloads")
        self.directory_edit.setText(default_directory)
        self.name_edit.setText(project_name())

    def _browse_directory(self):
        start = self.directory_edit.text() or os.path.expanduser("~")
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Seleccionar carpeta de salida", start, QtWidgets.QFileDialog.ShowDirsOnly
        )
        if directory:
            self.directory_edit.setText(directory)

    def _on_accept(self):
        if not os.path.isdir(self.directory_edit.text()):
            QtWidgets.QMessageBox.warning(self, "Carpeta inválida", "La carpeta de salida no existe.")
            return
        if not self.threemf_checkbox.isChecked() and not self.stl_checkbox.isChecked():
            QtWidgets.QMessageBox.warning(self, "Sin formato", "Marca 3MF o STL.")
            return
        self.accept()

    def get_params(self):
        formats = []
        if self.threemf_checkbox.isChecked():
            formats.append("3mf")
        if self.stl_checkbox.isChecked():
            formats.append("stl")
        return {
            "directory": self.directory_edit.text(),
            "formats": formats,
            "tolerance": self.tolerance_spin.value(),
            "create_zip": self.zip_checkbox.isChecked(),
            "base_name": self.name_edit.text().strip(),
        }


class ExportPlatesCommand:
    def GetResources(self):
        return {
            "Pixmap": "Nesting_ExportPlates.svg",
            "MenuText": QT_TRANSLATE_NOOP("ExportPlatesCommand", "Export 3MF Plates"),
            "ToolTip": QT_TRANSLATE_NOOP(
                "ExportPlatesCommand",
                "Export each Sheet_ as one arranged 3MF/STL plate for 3D printing.",
            ),
        }

    def Activated(self):
        layouts = find_layouts()
        if not layouts:
            QtWidgets.QMessageBox.warning(
                None,
                "Sin Layout",
                "No encontré una carpeta Layout_*. Selecciona el Layout en el árbol o crea uno con Nesting.",
            )
            return

        dialog = ExportPlatesDialog(layouts)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return

        params = dialog.get_params()
        total_sheets = sum(len(layout_sheets(layout_obj)) for layout_obj in layouts)
        progress = QtWidgets.QProgressDialog("Exportando platos...", "Cancelar", 0, total_sheets, None)
        progress.setWindowTitle("Export 3MF Plates")
        progress.setMinimumDuration(0)
        progress.setValue(0)

        def update_progress(done, total, message):
            progress.setMaximum(total)
            progress.setValue(done)
            progress.setLabelText(message)
            try:
                QtWidgets.QApplication.processEvents()
            except Exception:
                pass
            return not progress.wasCanceled()

        try:
            exported, errors, zip_path = export_layouts(
                layouts, progress_callback=update_progress, **params
            )
        except Exception as exc:
            progress.close()
            QtWidgets.QMessageBox.critical(None, "Error", str(exc))
            FreeCAD.Console.PrintError(f"Export 3MF Plates: {exc}\n")
            return
        progress.close()

        if errors:
            message = f"Se exportaron {len(exported)} plato(s).\n\nErrores:\n" + "\n".join(errors[:10])
            if len(errors) > 10:
                message += f"\n... y {len(errors) - 10} más"
            if zip_path:
                message += f"\n\nZIP:\n{zip_path}"
            QtWidgets.QMessageBox.warning(None, "Exportación con errores", message)
        else:
            message = f"Se exportaron {len(exported)} plato(s) en:\n{params['directory']}"
            if zip_path:
                message += f"\n\nZIP:\n{zip_path}"
            QtWidgets.QMessageBox.information(None, "Exportación completa", message)
        FreeCAD.Console.PrintMessage(
            f"Export 3MF Plates: {len(exported)} platos, {len(errors)} errores\n"
        )

    def IsActive(self):
        if not FreeCAD.ActiveDocument:
            return False
        selection = FreeCADGui.Selection.getSelection()
        if any(is_layout_group(obj) for obj in selection):
            return True
        return any(is_layout_group(obj) for obj in FreeCAD.ActiveDocument.Objects)


if FreeCAD.GuiUp:
    FreeCADGui.addCommand("Nesting_ExportPlates", ExportPlatesCommand())
