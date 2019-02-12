"""A setuptools based setup module.

See:
https://packaging.python.org/en/latest/distributing.html
https://github.com/pypa/sampleproject
"""

# Always prefer setuptools over distutils
from setuptools import setup, find_packages
from os import path
from io import open

from __version__ import version

here = path.abspath(path.dirname(__file__))

# Get the long description from the README file
with open(path.join(here, 'README.md'), encoding='utf-8') as f:
    long_description = f.read()

# Arguments marked as "Required" below must be included for upload to PyPI.
# Fields marked as "Optional" may be commented out.

setup(
    name='STORN-keras',
    version= version(),
    description='STORN implementation for keras',
    long_description=long_description,
    long_description_content_type='text/markdown',
    url='https://github.com/elias-fauser/STORN-keras', 
    author='Dominik Durner, Elias Fauser',
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Intended Audience :: Developers',
        'Topic :: Software Development',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
    ],
    keywords='storn keras anomaly detection',  # Optional
    packages=find_packages(exclude=['contrib', 'docs', 'tests']),  # Required
    python_requires='>=2.7, !=3.0.*, !=3.1.*, !=3.2.*, !=3.3.*, <4',
    install_requires=[
        'keras',
        'tensorflow',
        'theano',
        'scikit-learn'],  # Optional

    extras_require={  # Optional
        'dev': ['check-manifest'],
        'test': ['coverage'],
    },
    project_urls={  # Optional
        'Paper': 'https://arxiv.org/abs/1602.07109',
        'Additional': 'http://www.diva-portal.org/smash/get/diva2:896301/FULLTEXT01.pdf',
        'Source': 'https://github.com/durner/STORN-keras',
    },
)