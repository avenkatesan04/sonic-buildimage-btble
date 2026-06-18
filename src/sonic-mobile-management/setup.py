from setuptools import setup, find_packages

setup(
    name='sonic-mobile-management',
    version='1.0',
    description='BLE peripheral daemon for mobile switch management (SwitchMon)',
    license='Apache 2.0',
    author='SONiC Team',
    url='https://github.com/Azure/sonic-buildimage',
    packages=find_packages(),
    scripts=[
        'scripts/mobile-managementd',
    ],
    install_requires=[
        'bless',
        'dbus-fast',
    ],
    setup_requires=[
        'wheel',
    ],
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Environment :: Console',
        'Intended Audience :: System Administrators',
        'License :: OSI Approved :: Apache Software License',
        'Operating System :: POSIX :: Linux',
        'Programming Language :: Python :: 3',
        'Topic :: System :: Networking',
    ],
    keywords='sonic SONiC BLE mobile management SwitchMon',
)
