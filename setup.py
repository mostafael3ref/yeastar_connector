from setuptools import setup, find_packages

setup(
    name="yeastar_connector",
    version="0.0.1",
    description="Yeastar P-Series integration for ERPNext/Frappe",
    author="Mostafa EL-Areef",
    author_email="info@el3ref.com",
    packages=find_packages(),
    include_package_data=True,
    install_requires=["requests>=2.31.0"],
)
