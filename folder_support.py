"""Audiobook destination folders, confined to DOWNLOAD_FOLDER."""
import os
from flask import jsonify, request


def register_folders(app, root):
    root = os.path.realpath(root)

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
                if not name or name in ('.', '..') or name.startswith('.') or any(c in name for c in '/\\\x00'):
                    raise ValueError('Bitte einen einfachen Ordnernamen ohne Schrägstriche eingeben')
                parent = resolve(data.get('parent', ''))
                if not os.path.isdir(parent):
                    raise ValueError('Übergeordneter Ordner existiert nicht')
                relative = '/'.join(filter(None, [data.get('parent', ''), name]))
                destination = resolve(relative)
                os.mkdir(destination)
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
