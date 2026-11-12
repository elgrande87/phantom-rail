"""Audiobook destination folders, confined to DOWNLOAD_FOLDER."""
import os
from flask import jsonify, request

try:
    from system_checks import attach_system_checks
except Exception as _sc_err:
    attach_system_checks = None
    print(f"Warnung: system_checks konnte nicht geladen werden: {_sc_err}")


def register_folders(app, root):
    root = os.path.realpath(root)

    if attach_system_checks is not None:
        try:
            attach_system_checks(app, root)
        except Exception as exc:
            print(f"Warnung: attach_system_checks fehlgeschlagen: {exc}")

    def resolve(value):
        if not isinstance(value, str) or '\\' in value or '\x00' in value:
            raise ValueError('Ungültiger Ordnername')
        if os.path.isabs(value) or any(p in ('.', '..') or p.startswith('.') for p in value.split('/') if p):
            raise ValueError('Nur sichtbare Unterordner im Hörbuchverzeichnis sind erlaubt')
        path = os.path.realpath(os.path.join(root, value))
        if os.path.commonpath([root, path]) != root:
            raise ValueError('Ordner liegt außerhalb des Hörbuchverzeichnisses')
        return path

    @app.route('/folders', methods=['GET', 'POST'])
    def folders():
        try:
            if request.method == 'POST':
                data = request.get_json(silent=True) or {}
                name = data.get('name', '')
                if not isinstance(name, str):
                    raise ValueError('Ungültiger Ordnername')
                name = name.strip()
                if not name or name.startswith('.') or '\\' in name or '\x00' in name:
                    raise ValueError('Ungültiger Ordnername')
                parent_value = data.get('parent', '')
                parent = resolve(parent_value)
                if not os.path.isdir(parent):
                    raise ValueError('Übergeordneter Ordner existiert nicht')
                relative_name = name.strip('/')
                if not relative_name or any(part in ('', '.', '..') or part.startswith('.') for part in relative_name.split('/')):
                    raise ValueError('Bitte einen sichtbaren relativen Ordnerpfad ohne Punktordner eingeben')
                relative = '/'.join(filter(None, [parent_value, relative_name]))
                destination = resolve(relative)
                os.makedirs(destination, exist_ok=False)
                return jsonify({'folder': relative}), 201
            result = []
            for current, dirs, _ in os.walk(root, followlinks=False):
                dirs[:] = sorted((d for d in dirs if not d.startswith('.') and not os.path.islink(os.path.join(current, d))), key=str.casefold)
                for name in dirs:
                    result.append(os.path.relpath(os.path.join(current, name), root).replace(os.sep, '/'))
            return jsonify({'folders': sorted(result, key=str.casefold)})
        except FileExistsError:
            return jsonify({'error': 'Dieser Ordner existiert bereits'}), 409
        except ValueError as error:
            return jsonify({'error': str(error)}), 400
        except OSError:
            app.logger.exception('Ordnerzugriff fehlgeschlagen')
            return jsonify({'error': 'Ordnerzugriff fehlgeschlagen. SSD-Mount und Schreibrechte prüfen.'}), 500

    def destination(value):
        path = resolve(value)
        if not os.path.isdir(path):
            raise ValueError('Zielordner existiert nicht. Bitte zuerst anlegen oder neu auswählen.')
        return path

    return destination
