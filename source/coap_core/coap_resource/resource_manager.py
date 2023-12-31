import os


from source.coap_core.coap_utilities.coap_singleton import CoapSingletonBase
from source.coap_core.coap_resource.resource import Resource


class ResourceManager(CoapSingletonBase):

    def __init__(self):
        self.initialized = True
        self.__resources: list = []
        self.__default_resource = None
        self.__root_path = None

    def set_root_path(self, root_path: str):
        self.__root_path = root_path
        try:
            os.makedirs(root_path)
        except FileExistsError:
            pass

    def add_resource(self, resource: Resource):
        resource.set_root_path(self.__root_path)
        self.__resources.append(resource)

    def add_default_resource(self, resource: Resource):
        resource.set_root_path(self.__root_path)
        self.__default_resource = resource

    def get_resource(self, name: str) -> Resource | None:
        for resource in self.__resources:
            if resource.get_name() == name:
                return resource
        return None

    def get_default_resource(self):
        return self.__default_resource

    def discover_resources(self):
        pass
