#!/bin/zsh
export LANG=ru_RU.UTF-8
cd "${0:A:h}" || exit 1
clear
print 'Запуск сервера MAX-бота…'
print ''
./.venv/bin/python ./launch_server.py
result=$?
print ''
if [[ $result -ne 0 ]]; then
    print 'Запуск требует внимания. Сообщение об ошибке указано выше.'
fi
read -k 1 '?Нажмите любую клавишу, чтобы закрыть запуск…'
print ''
exit $result
