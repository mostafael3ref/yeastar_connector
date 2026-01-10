from setuptools import setup, find_packages

setup(
    name="yeastar_connector",
    version="0.0.1",
    description="Private Yeastar P-Series (P570) integration for ERPNext/Frappe",
    author="Mostafa EL-Areef",
    author_email="info@el3ref.com",
    packages=find_packages(),
    include_package_data=True,
    zip_safe=False,
)
