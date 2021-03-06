# This file is part of Tautulli.
#
#  Tautulli is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Tautulli is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Tautulli.  If not, see <http://www.gnu.org/licenses/>.

import arrow
import os
import sqlite3
import shutil
import threading
import time

import plexpy
import helpers
import logger

FILENAME = "tautulli.db"
db_lock = threading.Lock()


def integrity_check():
    monitor_db = MonitorDatabase()
    result = monitor_db.select_single('PRAGMA integrity_check')
    return result


def clear_table(table=None):
    if table:
        monitor_db = MonitorDatabase()

        logger.debug(u"Tautulli Database :: Clearing database table '%s'." % table)
        try:
            monitor_db.action('DELETE FROM %s' % table)
            monitor_db.action('VACUUM')
            return True
        except Exception as e:
            logger.error(u"Tautulli Database :: Failed to clear database table '%s': %s." % (table, e))
            return False


def delete_sessions():
    logger.info(u"Tautulli Database :: Clearing temporary sessions from database.")
    return clear_table('sessions')


def delete_recently_added():
    logger.info(u"Tautulli Database :: Clearing recently added items from database.")
    return clear_table('recently_added')


def delete_rows_from_table(table, row_ids):
    if row_ids and isinstance(row_ids, basestring):
        row_ids = map(helpers.cast_to_int, row_ids.split(','))

    if row_ids:
        logger.info(u"Tautulli Database :: Deleting row ids %s from %s database table", row_ids, table)
        query = "DELETE FROM " + table + " WHERE id IN (%s) " % ','.join(['?'] * len(row_ids))
        monitor_db = MonitorDatabase()

        try:
            monitor_db.action(query, row_ids)
            return True
        except Exception as e:
            logger.error(u"Tautulli Database :: Failed to delete rows from %s database table: %s" % (table, row_ids))
            return False

    return True


def delete_session_history_rows(row_ids=None):
    success = []
    for table in ('session_history', 'session_history_media_info', 'session_history_metadata'):
        success.append(delete_rows_from_table(table=table, row_ids=row_ids))
    return all(success)


def delete_user_history(user_id=None):
    if str(user_id).isdigit():
        monitor_db = MonitorDatabase()

        # Get all history associated with the user_id
        result = monitor_db.select('SELECT id FROM session_history WHERE user_id = ?',
                                   [user_id])
        row_ids = [row['id'] for row in result]

        logger.info(u"Tautulli Database :: Deleting all history for user_id %s from database." % user_id)
        return delete_session_history_rows(row_ids=row_ids)


def delete_library_history(section_id=None):
    if str(section_id).isdigit():
        monitor_db = MonitorDatabase()

        # Get all history associated with the section_id
        result = monitor_db.select('SELECT session_history.id FROM session_history '
                                   'JOIN session_history_metadata ON session_history.id = session_history_metadata.id '
                                   'WHERE session_history_metadata.section_id = ?',
                                   [section_id])
        row_ids = [row['id'] for row in result]

        logger.info(u"Tautulli Database :: Deleting all history for library section_id %s from database." % section_id)
        return delete_session_history_rows(row_ids=row_ids)


def db_filename(filename=FILENAME):
    """ Returns the filepath to the db """

    return os.path.join(plexpy.DATA_DIR, filename)


def make_backup(cleanup=False, scheduler=False):
    """ Makes a backup of db, removes all but the last 5 backups """

    # Check the integrity of the database first
    integrity = (integrity_check()['integrity_check'] == 'ok')

    corrupt = ''
    if not integrity:
        corrupt = '.corrupt'
        plexpy.NOTIFY_QUEUE.put({'notify_action': 'on_plexpydbcorrupt'})

    if scheduler:
        backup_file = 'tautulli.backup-{}{}.sched.db'.format(arrow.now().format('YYYYMMDDHHmmss'), corrupt)
    else:
        backup_file = 'tautulli.backup-{}{}.db'.format(arrow.now().format('YYYYMMDDHHmmss'), corrupt)
    backup_folder = plexpy.CONFIG.BACKUP_DIR
    backup_file_fp = os.path.join(backup_folder, backup_file)

    # In case the user has deleted it manually
    if not os.path.exists(backup_folder):
        os.makedirs(backup_folder)

    db = MonitorDatabase()
    db.connection.execute('begin immediate')
    shutil.copyfile(db_filename(), backup_file_fp)
    db.connection.rollback()

    # Only cleanup if the database integrity is okay
    if cleanup and integrity:
        now = time.time()
        # Delete all scheduled backup older than BACKUP_DAYS.
        for root, dirs, files in os.walk(backup_folder):
            db_files = [os.path.join(root, f) for f in files if f.endswith('.sched.db')]
            for file_ in db_files:
                if os.stat(file_).st_mtime < now - plexpy.CONFIG.BACKUP_DAYS * 86400:
                    try:
                        os.remove(file_)
                    except OSError as e:
                        logger.error(u"Tautulli Database :: Failed to delete %s from the backup folder: %s" % (file_, e))

    if backup_file in os.listdir(backup_folder):
        logger.debug(u"Tautulli Database :: Successfully backed up %s to %s" % (db_filename(), backup_file))
        return True
    else:
        logger.error(u"Tautulli Database :: Failed to backup %s to %s" % (db_filename(), backup_file))
        return False


def get_cache_size():
    # This will protect against typecasting problems produced by empty string and None settings
    if not plexpy.CONFIG.CACHE_SIZEMB:
        # sqlite will work with this (very slowly)
        return 0
    return int(plexpy.CONFIG.CACHE_SIZEMB)


def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]

    return d


class MonitorDatabase(object):

    def __init__(self, filename=FILENAME):
        self.filename = filename
        self.connection = sqlite3.connect(db_filename(filename), timeout=20)
        # Set database synchronous mode (default NORMAL)
        self.connection.execute("PRAGMA synchronous = %s" % plexpy.CONFIG.SYNCHRONOUS_MODE)
        # Set database journal mode (default WAL)
        self.connection.execute("PRAGMA journal_mode = %s" % plexpy.CONFIG.JOURNAL_MODE)
        # Set database cache size (default 32MB)
        self.connection.execute("PRAGMA cache_size = -%s" % (get_cache_size() * 1024))
        self.connection.row_factory = dict_factory

    def action(self, query, args=None, return_last_id=False):
        if query is None:
            return

        with db_lock:
            sql_result = None
            attempts = 0

            while attempts < 5:
                try:
                    with self.connection as c:
                        if args is None:
                            sql_result = c.execute(query)
                        else:
                            sql_result = c.execute(query, args)
                    # Our transaction was successful, leave the loop
                    break

                except sqlite3.OperationalError as e:
                    if "unable to open database file" in e or "database is locked" in e:
                        logger.warn(u"Tautulli Database :: Database Error: %s", e)
                        attempts += 1
                        time.sleep(1)
                    else:
                        logger.error(u"Tautulli Database :: Database error: %s", e)
                        raise

                except sqlite3.DatabaseError as e:
                    logger.error(u"Tautulli Database :: Fatal Error executing %s :: %s", query, e)
                    raise

            return sql_result

    def select(self, query, args=None):

        sql_results = self.action(query, args).fetchall()

        if sql_results is None or sql_results == [None]:
            return []

        return sql_results

    def select_single(self, query, args=None):

        sql_results = self.action(query, args).fetchone()

        if sql_results is None or sql_results == "":
            return {}

        return sql_results

    def upsert(self, table_name, value_dict, key_dict):

        trans_type = 'update'
        changes_before = self.connection.total_changes

        gen_params = lambda my_dict: [x + " = ?" for x in my_dict.keys()]

        update_query = "UPDATE " + table_name + " SET " + ", ".join(gen_params(value_dict)) + \
                       " WHERE " + " AND ".join(gen_params(key_dict))

        self.action(update_query, value_dict.values() + key_dict.values())

        if self.connection.total_changes == changes_before:
            trans_type = 'insert'
            insert_query = (
                "INSERT INTO " + table_name + " (" + ", ".join(value_dict.keys() + key_dict.keys()) + ")" +
                " VALUES (" + ", ".join(["?"] * len(value_dict.keys() + key_dict.keys())) + ")"
            )
            try:
                self.action(insert_query, value_dict.values() + key_dict.values())
            except sqlite3.IntegrityError:
                logger.info(u"Tautulli Database :: Queries failed: %s and %s", update_query, insert_query)

        # We want to know if it was an update or insert
        return trans_type

    def last_insert_id(self):
        # Get the last insert row id
        result = self.select_single(query='SELECT last_insert_rowid() AS last_id')
        if result:
            return result.get('last_id', None)